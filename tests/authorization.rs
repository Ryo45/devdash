//! A private bus stands in for the system bus. Never contacts a real system service.
use anyhow::{Result, ensure};
use devdash::{
    engine::Action,
    ipc::{self, Request, Snapshot},
};
use std::{
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};
use tokio::io::{AsyncBufReadExt, BufReader};
use zbus::zvariant::OwnedObjectPath;
struct Manager(Arc<AtomicUsize>);
#[zbus::interface(name = "org.freedesktop.systemd1.Manager")]
impl Manager {
    fn subscribe(&self) {}
    fn load_unit(&self, _name: &str) -> OwnedObjectPath {
        "/org/freedesktop/systemd1/unit/same".try_into().unwrap()
    }
    fn start_unit(
        &self,
        _name: &str,
        _mode: &str,
        #[zbus(header)] header: zbus::message::Header<'_>,
    ) -> zbus::fdo::Result<OwnedObjectPath> {
        assert!(
            header
                .primary()
                .flags()
                .contains(zbus::message::Flags::AllowInteractiveAuth)
        );
        self.0.fetch_add(1, Ordering::SeqCst);
        Err(zbus::fdo::Error::AccessDenied(
            "test policy denies manage-units".into(),
        ))
    }
}
struct Unit;
#[zbus::interface(name = "org.freedesktop.systemd1.Unit")]
impl Unit {
    #[zbus(property)]
    fn id(&self) -> &str {
        "same.service"
    }
    #[zbus(property)]
    fn load_state(&self) -> &str {
        "loaded"
    }
    #[zbus(property)]
    fn active_state(&self) -> &str {
        "inactive"
    }
    #[zbus(property)]
    fn sub_state(&self) -> &str {
        "dead"
    }
}
#[tokio::test]
async fn system_authorization_uses_polkit_flags_and_reports_denial_without_retries() -> Result<()> {
    let mut bus = tokio::process::Command::new("dbus-daemon")
        .args(["--session", "--nofork", "--print-address=1"])
        .stdout(std::process::Stdio::piped())
        .kill_on_drop(true)
        .spawn()?;
    let mut address = String::new();
    tokio::time::timeout(
        Duration::from_secs(5),
        BufReader::new(bus.stdout.take().unwrap()).read_line(&mut address),
    )
    .await??;
    let calls = Arc::new(AtomicUsize::new(0));
    let _server = zbus::connection::Builder::address(address.trim())?
        .name("org.freedesktop.systemd1")?
        .serve_at("/org/freedesktop/systemd1", Manager(calls.clone()))?
        .serve_at("/org/freedesktop/systemd1/unit/same", Unit)?
        .build()
        .await?;
    let dir = tempfile::tempdir()?;
    let config = dir.path().join("config.yaml");
    let socket = dir.path().join("core.sock");
    std::fs::write(
        &config,
        "workloads:\n  system_service: {backend: systemd, scope: system, unit: same.service}\ngroups: {}\n",
    )?;
    let mut daemon = tokio::process::Command::new(env!("CARGO_BIN_EXE_devdash"))
        .env("DBUS_SYSTEM_BUS_ADDRESS", address.trim())
        .env("DBUS_SESSION_BUS_ADDRESS", address.trim())
        .args([
            "--socket",
            socket.to_str().unwrap(),
            "daemon",
            "--config",
            config.to_str().unwrap(),
            "--state",
            dir.path().join("state.json").to_str().unwrap(),
        ])
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .kill_on_drop(true)
        .spawn()?;
    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            if ipc::request(&socket, Request::Snapshot).await.is_ok() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await?;
    ipc::request(
        &socket,
        Request::Operate {
            target: "system_service".into(),
            group: false,
            action: Action::Start,
        },
    )
    .await?;
    let snapshot = tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            let s: Snapshot =
                serde_json::from_value(ipc::request(&socket, Request::Snapshot).await?.data)?;
            if s.run.status == "failed" {
                return Ok::<_, anyhow::Error>(s);
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await??;
    ensure!(
        snapshot
            .run
            .progress
            .iter()
            .filter_map(|p| p.error.as_ref())
            .any(|e| e.contains("Polkit")
                && e.contains("system:same.service")
                && e.contains("AccessDenied"))
    );
    ensure!(
        calls.load(Ordering::SeqCst) == 1,
        "no retry/supervisor allowed"
    );
    daemon.kill().await?;
    bus.kill().await?;
    Ok(())
}
