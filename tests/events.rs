//! Isolated systemd protocol tests. No host systemd manager or units are used.
use anyhow::{Result, ensure};
use devdash::{
    engine::Action,
    ipc::{self, Request, Snapshot},
    model::Scope,
    monitor::{Correlation, EventKind},
};
use std::{
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    time::Duration,
};
use tokio::{
    io::{AsyncBufReadExt, BufReader},
    process::Child,
    sync::Notify,
};
use zbus::{Connection, object_server::SignalEmitter, zvariant::OwnedObjectPath};
#[derive(Default)]
struct Control {
    calls: AtomicUsize,
    subscriptions: AtomicUsize,
    pause_subscribe: AtomicBool,
    subscribing: Notify,
    resume_subscribe: Notify,
    active: AtomicBool,
    mode: AtomicUsize,
    pause_read: AtomicBool,
    pause_load: AtomicBool,
    loading: Notify,
    resume_load: Notify,
    reading: Notify,
    resume: Notify,
}
struct Manager(Arc<Control>);
#[zbus::interface(name = "org.freedesktop.systemd1.Manager")]
impl Manager {
    async fn subscribe(&self) {
        if self.0.pause_subscribe.swap(false, Ordering::SeqCst) {
            self.0.subscribing.notify_one();
            self.0.resume_subscribe.notified().await;
        }
        self.0.subscriptions.fetch_add(1, Ordering::SeqCst);
    }
    async fn load_unit(&self, _name: &str) -> OwnedObjectPath {
        if self.0.pause_load.swap(false, Ordering::SeqCst) {
            self.0.loading.notify_one();
            self.0.resume_load.notified().await;
        }
        "/org/freedesktop/systemd1/unit/probe".try_into().unwrap()
    }
    async fn start_unit(
        &self,
        name: &str,
        _mode: &str,
        #[zbus(signal_emitter)] emitter: SignalEmitter<'_>,
    ) -> zbus::fdo::Result<OwnedObjectPath> {
        let number = self.0.calls.fetch_add(1, Ordering::SeqCst) + 1;
        let path: OwnedObjectPath = format!("/org/freedesktop/systemd1/job/{number}")
            .try_into()
            .unwrap();
        let mode = self.0.mode.load(Ordering::SeqCst);
        if mode == 1 {
            return Ok(path);
        }
        if mode == 2 {
            for i in 0..300 {
                Self::unit_new(
                    &emitter,
                    &format!("noise-{i}.service"),
                    "/org/freedesktop/systemd1/unit/noise".try_into().unwrap(),
                )
                .await?;
                Self::job_removed(
                    &emitter,
                    1000 + i,
                    format!("/org/freedesktop/systemd1/job/noise{i}")
                        .try_into()
                        .unwrap(),
                    "noise.service",
                    "done",
                )
                .await?;
            }
        }
        Self::job_removed(&emitter, number as u32, path.clone(), name, "done").await?;
        if mode == 0 {
            tokio::time::sleep(Duration::from_millis(250)).await;
        }
        Ok(path)
    }
    #[zbus(signal)]
    async fn job_removed(
        emitter: &SignalEmitter<'_>,
        id: u32,
        job: OwnedObjectPath,
        unit: &str,
        result: &str,
    ) -> zbus::Result<()>;
    #[zbus(signal)]
    async fn unit_new(
        emitter: &SignalEmitter<'_>,
        unit: &str,
        path: OwnedObjectPath,
    ) -> zbus::Result<()>;
    #[zbus(signal)]
    async fn reloading(emitter: &SignalEmitter<'_>, active: bool) -> zbus::Result<()>;
}
struct Unit(Arc<Control>);
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
    async fn active_state(&self) -> &str {
        if self.0.pause_read.swap(false, Ordering::SeqCst) {
            self.0.reading.notify_one();
            self.0.resume.notified().await;
        }
        if self.0.active.load(Ordering::SeqCst) {
            "active"
        } else {
            "inactive"
        }
    }
    #[zbus(property)]
    fn sub_state(&self) -> &str {
        "dead"
    }
}
fn write_bus_config(dir: &Path, name: &str) -> Result<PathBuf> {
    let config = dir.join(format!("{name}.conf"));
    // No service directories: no host service activation, including on owner loss.
    std::fs::write(
        &config,
        format!(
            "<busconfig><type>session</type><listen>unix:path={}</listen><auth>EXTERNAL</auth><policy context=\"default\"><allow own=\"*\"/><allow send_destination=\"*\"/><allow receive_sender=\"*\"/></policy></busconfig>",
            dir.join(format!("{name}.sock")).display()
        ),
    )?;
    Ok(config)
}
async fn launch_bus(config: &Path) -> Result<(Child, String)> {
    let mut bus = tokio::process::Command::new("dbus-daemon")
        .arg(format!("--config-file={}", config.display()))
        .args(["--nofork", "--print-address=1"])
        .stdout(std::process::Stdio::piped())
        .kill_on_drop(true)
        .spawn()?;
    let mut address = String::new();
    tokio::time::timeout(
        Duration::from_secs(5),
        BufReader::new(bus.stdout.take().unwrap()).read_line(&mut address),
    )
    .await??;
    // Like /run/user/$UID/bus: a stable endpoint, not a GUID pinned to the old
    // dbus-daemon instance. Keep explicit GUID authentication semantics intact.
    Ok((bus, address.trim().split(",guid=").next().unwrap().into()))
}
async fn manager_server(address: &str, control: Arc<Control>, named: bool) -> Result<Connection> {
    let server = zbus::connection::Builder::address(address)?
        .serve_at("/org/freedesktop/systemd1", Manager(control.clone()))?
        .serve_at("/org/freedesktop/systemd1/unit/probe", Unit(control))?
        .build()
        .await?;
    if named {
        server.request_name("org.freedesktop.systemd1").await?;
    }
    Ok(server)
}
struct Fixture {
    control: Arc<Control>,
    server: Connection,
    system_server: Connection,
    system_control: Arc<Control>,
    address: String,
    bus_config: PathBuf,
    socket: PathBuf,
    _daemon: Child,
    bus: Child,
    _system_bus: Child,
    _dir: tempfile::TempDir,
}
impl Fixture {
    async fn start(paused_baseline: bool) -> Result<Self> {
        Self::start_config(paused_baseline, false, true).await
    }
    async fn start_config(paused_baseline: bool, grouped: bool, named: bool) -> Result<Self> {
        let dir = tempfile::tempdir()?;
        let conf = write_bus_config(dir.path(), "user")?;
        let system_conf = write_bus_config(dir.path(), "system")?;
        let (bus, address) = launch_bus(&conf).await?;
        let (system_bus, system_address) = launch_bus(&system_conf).await?;
        let control = Arc::new(Control::default());
        control.pause_read.store(paused_baseline, Ordering::SeqCst);
        let server = manager_server(&address, control.clone(), named).await?;
        let system_control = Arc::new(Control::default());
        let system_server = manager_server(&system_address, system_control.clone(), named).await?;
        let config = dir.path().join("config.yaml");
        let socket = dir.path().join("core.sock");
        std::fs::write(
            &config,
            format!(
                "workloads:\n  probe: {{backend: systemd, scope: user, unit: same.service}}\n  system_probe: {{backend: systemd, scope: system, unit: same.service}}\n{}",
                if grouped {
                    "groups:\n  gated:\n    steps: [{workload: probe}, {gate: ready}, {workload: system_probe}]\ngates:\n  ready: {type: manual}\n"
                } else {
                    "groups: {}\n"
                }
            ),
        )?;
        let daemon = tokio::process::Command::new(env!("CARGO_BIN_EXE_devdash"))
            .env("DBUS_SESSION_BUS_ADDRESS", address.trim())
            .env("DBUS_SYSTEM_BUS_ADDRESS", system_address.trim())
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
        Ok(Self {
            control,
            server,
            system_server,
            system_control,
            address,
            bus_config: conf,
            socket,
            _daemon: daemon,
            bus,
            _system_bus: system_bus,
            _dir: dir,
        })
    }
    async fn snapshot(&self) -> Result<Snapshot> {
        Ok(serde_json::from_value(
            ipc::request(&self.socket, Request::Snapshot).await?.data,
        )?)
    }
    async fn ready(&self) -> Result<()> {
        tokio::time::timeout(Duration::from_secs(5), async {
            while self.snapshot().await.is_err() {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await?;
        Ok(())
    }
    async fn start_workload(&self) -> Result<()> {
        self.operate("probe", false).await
    }
    async fn operate(&self, target: &str, group: bool) -> Result<()> {
        ipc::request(
            &self.socket,
            Request::Operate {
                target: target.into(),
                group,
                action: Action::Start,
            },
        )
        .await?;
        Ok(())
    }
    async fn wait_for(&self, predicate: impl Fn(&Snapshot) -> bool) -> Result<Snapshot> {
        tokio::time::timeout(Duration::from_secs(12), async {
            loop {
                let s = self.snapshot().await?;
                if predicate(&s) {
                    return Ok::<_, anyhow::Error>(s);
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await?
    }
    async fn emit_job(&self, id: u32) -> Result<()> {
        let emitter = SignalEmitter::new(&self.server, "/org/freedesktop/systemd1")?;
        Manager::job_removed(
            &emitter,
            id,
            format!("/org/freedesktop/systemd1/job/{id}").try_into()?,
            "same.service",
            "done",
        )
        .await?;
        Ok(())
    }
}
#[tokio::test]
async fn delayed_reply_is_correlated_without_false_external_warning() -> Result<()> {
    let f = Fixture::start(false).await?;
    f.ready().await?;
    f.start_workload().await?;
    let snapshot = f
        .wait_for(|s| {
            s.run.status == "completed" && s.events.iter().any(|e| e.kind == EventKind::Job)
        })
        .await?;
    let jobs: Vec<_> = snapshot
        .events
        .iter()
        .filter(|e| e.kind == EventKind::Job)
        .collect();
    ensure!(jobs.len() == 1);
    ensure!(jobs[0].correlation == Correlation::Requested);
    ensure!(jobs[0].level == "info");
    ensure!(jobs[0].job_result.as_deref() == Some("done"));
    Ok(())
}
#[tokio::test]
async fn manager_owner_loss_finishes_pending_operation_and_releases_busy() -> Result<()> {
    let f = Fixture::start(false).await?;
    f.ready().await?;
    f.control.mode.store(1, Ordering::SeqCst);
    f.start_workload().await?;
    tokio::time::timeout(Duration::from_secs(2), async {
        while f.control.calls.load(Ordering::SeqCst) < 1 {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await?;
    ipc::request(&f.socket, Request::Cancel).await?;
    f.server.release_name("org.freedesktop.systemd1").await?;
    let failed = f
        .wait_for(|s| s.run.status == "failed" && !s.monitoring[&Scope::User].available)
        .await?;
    ensure!(failed.run.progress.iter().any(|p| {
        p.error
            .as_ref()
            .is_some_and(|e| e.contains("outcome unknown"))
    }));
    ensure!(
        failed
            .events
            .iter()
            .any(|e| e.kind == EventKind::Disconnected)
    );
    f.start_workload().await?;
    f.wait_for(|s| s.run.status == "failed").await?;
    ensure!(
        f.control.calls.load(Ordering::SeqCst) == 1,
        "no new request after owner loss"
    );
    Ok(())
}
#[tokio::test]
async fn burst_of_more_than_64_events_does_not_stall_control() -> Result<()> {
    let f = Fixture::start(false).await?;
    f.ready().await?;
    f.control.mode.store(2, Ordering::SeqCst);
    f.start_workload().await?;
    let done = f.wait_for(|s| s.run.status == "completed").await?;
    ensure!(done.monitoring[&Scope::User].available);
    Ok(())
}
#[tokio::test]
async fn events_during_initial_baseline_are_buffered() -> Result<()> {
    let f = Fixture::start(true).await?;
    tokio::time::timeout(Duration::from_secs(3), f.control.reading.notified()).await?;
    f.emit_job(91).await?;
    tokio::time::sleep(Duration::from_millis(30)).await;
    f.control.resume.notify_one();
    f.ready().await?;
    let s = f
        .wait_for(|s| {
            s.events
                .iter()
                .any(|e| e.job_path.as_deref() == Some("/org/freedesktop/systemd1/job/91"))
        })
        .await?;
    ensure!(s.events.iter().filter(|e| e.kind == EventKind::Job).count() == 1);
    Ok(())
}
#[tokio::test]
async fn slow_poll_does_not_delay_event_drain_and_reload_warns_during_operation() -> Result<()> {
    let f = Fixture::start(false).await?;
    f.ready().await?;
    f.control.pause_read.store(true, Ordering::SeqCst);
    tokio::time::timeout(Duration::from_secs(3), f.control.reading.notified()).await?;
    f.emit_job(92).await?;
    f.wait_for(|s| {
        s.events
            .iter()
            .any(|e| e.job_path.as_deref() == Some("/org/freedesktop/systemd1/job/92"))
    })
    .await?;
    f.control.resume.notify_one();
    f.control.mode.store(1, Ordering::SeqCst);
    f.start_workload().await?;
    tokio::time::timeout(Duration::from_secs(2), async {
        while f.control.calls.load(Ordering::SeqCst) < 1 {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await?;
    let emitter = SignalEmitter::new(&f.server, "/org/freedesktop/systemd1")?;
    Manager::reloading(&emitter, true).await?;
    Manager::reloading(&emitter, false).await?;
    let s = f
        .wait_for(|s| {
            s.events
                .iter()
                .any(|e| e.kind == EventKind::Reloaded && e.level == "warning")
        })
        .await?;
    ensure!(
        s.events
            .iter()
            .any(|e| e.kind == EventKind::Reloading && e.level == "warning")
    );
    Ok(())
}

#[tokio::test]
async fn replacement_owner_resubscribes_before_accepting_new_work_and_rebaselines() -> Result<()> {
    let mut f = Fixture::start(false).await?;
    f.ready().await?;
    let old_generation = f.snapshot().await?.observations["probe"].manager_generation;
    f.control.mode.store(1, Ordering::SeqCst);
    f.start_workload().await?;
    f.wait_for(|_| f.control.calls.load(Ordering::SeqCst) == 1)
        .await?;
    f.server.release_name("org.freedesktop.systemd1").await?;
    f.wait_for(|s| s.run.status == "failed" && !s.monitoring[&Scope::User].available)
        .await?;
    // A different manager remains usable while user reconnects.
    f.operate("system_probe", false).await?;
    f.wait_for(|s| s.run.status == "completed").await?;
    ensure!(f.system_control.calls.load(Ordering::SeqCst) == 1);
    let next = Arc::new(Control::default());
    next.active.store(true, Ordering::SeqCst);
    next.pause_subscribe.store(true, Ordering::SeqCst);
    let replacement = manager_server(&f.address, next.clone(), true).await?;
    tokio::time::timeout(Duration::from_secs(5), next.subscribing.notified()).await?;
    ensure!(!f.snapshot().await?.monitoring[&Scope::User].available);
    f.start_workload().await?;
    f.wait_for(|s| s.run.status == "failed").await?;
    ensure!(
        next.calls.load(Ordering::SeqCst) == 0,
        "must reject while Subscribe is pending"
    );
    next.resume_subscribe.notify_one();
    f.server = replacement;
    let restored = f
        .wait_for(|s| {
            s.monitoring[&Scope::User].available
                && s.observations["probe"].manager_generation != old_generation
                && s.observations["probe"].state == "active"
        })
        .await?;
    ensure!(next.subscriptions.load(Ordering::SeqCst) == 2);
    ensure!(restored.monitor_gap.is_some());
    ensure!(
        restored
            .events
            .iter()
            .any(|e| e.kind == EventKind::Reconnected && e.scope == Some(Scope::User))
    );
    ensure!(restored.events.iter().any(
        |e| e.kind == EventKind::ObservationRestored && e.workload.as_deref() == Some("probe")
    ));
    ensure!(
        !restored
            .events
            .iter()
            .any(|e| e.kind == EventKind::StateChanged && e.workload.as_deref() == Some("probe")),
        "no inferred change across manager generations"
    );
    ensure!(
        next.calls.load(Ordering::SeqCst) == 0,
        "interrupted operation must not be replayed"
    );
    f.emit_job(99).await?;
    f.wait_for(|s| {
        s.events
            .iter()
            .any(|e| e.job_path.as_deref() == Some("/org/freedesktop/systemd1/job/99"))
    })
    .await?;
    f.start_workload().await?;
    f.wait_for(|s| s.run.status == "completed").await?;
    ensure!(next.calls.load(Ordering::SeqCst) == 1);
    ensure!(
        f.control.calls.load(Ordering::SeqCst) == 1,
        "old owner is never reused"
    );
    Ok(())
}

#[tokio::test]
async fn disconnected_bus_is_reopened_at_same_address() -> Result<()> {
    let mut f = Fixture::start(false).await?;
    f.ready().await?;
    f.control.mode.store(1, Ordering::SeqCst);
    f.start_workload().await?;
    f.wait_for(|_| f.control.calls.load(Ordering::SeqCst) == 1)
        .await?;
    f.bus.kill().await?;
    f.bus.wait().await?;
    let lost = f
        .wait_for(|s| s.run.status == "failed" && !s.monitoring[&Scope::User].available)
        .await?;
    ensure!(lost.monitoring[&Scope::System].available);
    let socket = f._dir.path().join("user.sock");
    if socket.exists() {
        std::fs::remove_file(&socket)?;
    }
    let (bus, address) = launch_bus(&f.bus_config).await?;
    f.bus = bus;
    let next = Arc::new(Control::default());
    f.server = manager_server(&address, next.clone(), true).await?;
    f.wait_for(|s| s.monitoring[&Scope::User].available).await?;
    ensure!(next.calls.load(Ordering::SeqCst) == 0);
    f.start_workload().await?;
    f.wait_for(|s| s.run.status == "completed").await?;
    ensure!(next.subscriptions.load(Ordering::SeqCst) == 2);
    ensure!(next.calls.load(Ordering::SeqCst) == 1);
    Ok(())
}

#[tokio::test]
async fn managers_absent_at_startup_recover_without_restarting_core() -> Result<()> {
    let f = Fixture::start_config(false, false, false).await?;
    f.ready().await?;
    ensure!(
        f.snapshot()
            .await?
            .monitoring
            .values()
            .all(|h| !h.available)
    );
    f.start_workload().await?;
    f.wait_for(|s| s.run.status == "failed").await?;
    f.server.request_name("org.freedesktop.systemd1").await?;
    f.system_server
        .request_name("org.freedesktop.systemd1")
        .await?;
    f.wait_for(|s| s.monitoring.values().all(|h| h.available))
        .await?;
    ensure!(f.control.calls.load(Ordering::SeqCst) == 0);
    ensure!(f.system_control.calls.load(Ordering::SeqCst) == 0);
    f.start_workload().await?;
    f.wait_for(|s| s.run.status == "completed").await?;
    Ok(())
}

#[tokio::test]
async fn group_waiting_at_gate_is_not_resumed_after_manager_replacement() -> Result<()> {
    let mut f = Fixture::start_config(false, true, true).await?;
    f.ready().await?;
    f.operate("gated", true).await?;
    f.wait_for(|s| s.run.waiting_gate.as_deref() == Some("ready"))
        .await?;
    f.server.release_name("org.freedesktop.systemd1").await?;
    let stopped = f.wait_for(|s| s.run.status == "failed").await?;
    ensure!(
        stopped
            .run
            .progress
            .iter()
            .any(|p| p.step == "system_probe" && p.status == "skipped")
    );
    let next = Arc::new(Control::default());
    f.server = manager_server(&f.address, next.clone(), true).await?;
    f.wait_for(|s| s.monitoring[&Scope::User].available).await?;
    ipc::request(
        &f.socket,
        Request::Approve {
            gate: "ready".into(),
        },
    )
    .await?;
    tokio::time::sleep(Duration::from_millis(300)).await;
    ensure!(f.snapshot().await?.run.status == "failed");
    ensure!(f.system_control.calls.load(Ordering::SeqCst) == 0);
    ensure!(next.calls.load(Ordering::SeqCst) == 0);
    // A fresh, explicitly requested sequence can use the restored manager.
    f.operate("gated", true).await?;
    f.wait_for(|s| s.run.status == "completed").await?;
    ensure!(f.system_control.calls.load(Ordering::SeqCst) == 1);
    Ok(())
}

#[tokio::test]
async fn repeated_registered_unit_metadata_signals_do_not_displace_jobs() -> Result<()> {
    let f = Fixture::start(false).await?;
    f.ready().await?;
    let emitter = SignalEmitter::new(&f.server, "/org/freedesktop/systemd1")?;
    for _ in 0..100 {
        Manager::unit_new(
            &emitter,
            "same.service",
            "/org/freedesktop/systemd1/unit/probe".try_into()?,
        )
        .await?;
    }
    f.emit_job(100).await?;
    let s = f
        .wait_for(|s| {
            s.events
                .iter()
                .any(|e| e.job_path.as_deref() == Some("/org/freedesktop/systemd1/job/100"))
        })
        .await?;
    ensure!(
        s.events
            .iter()
            .filter(|e| matches!(e.kind, EventKind::UnitNew | EventKind::UnitRemoved))
            .count()
            == 1
    );
    ensure!(s.monitor_gap.is_none());
    Ok(())
}

#[tokio::test]
async fn manager_loss_during_operation_preparation_releases_busy_without_starting() -> Result<()> {
    let f = Fixture::start(false).await?;
    f.ready().await?;
    // Hold the periodic poll so only the new operation can enter LoadUnit.
    f.control.pause_read.store(true, Ordering::SeqCst);
    tokio::time::timeout(Duration::from_secs(3), f.control.reading.notified()).await?;
    f.control.pause_load.store(true, Ordering::SeqCst);
    f.start_workload().await?;
    tokio::time::timeout(Duration::from_secs(2), f.control.loading.notified()).await?;
    f.server.release_name("org.freedesktop.systemd1").await?;
    let failed = tokio::time::timeout(
        Duration::from_secs(2),
        f.wait_for(|s| s.run.status == "failed"),
    )
    .await??;
    ensure!(failed.run.progress.iter().any(|p| {
        p.error
            .as_ref()
            .is_some_and(|e| e.contains("during preparation"))
    }));
    ensure!(f.control.calls.load(Ordering::SeqCst) == 0);
    f.control.resume_load.notify_one();
    f.control.resume.notify_one();
    Ok(())
}
