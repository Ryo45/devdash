use anyhow::Result;
use async_trait::async_trait;
use devdash::{
    engine::{Action, Backend, Engine, Observation},
    ipc::{self, Request, Snapshot},
    model::{Config, Workload},
};
use std::{sync::Arc, time::Duration};
struct Fake;
#[async_trait]
impl Backend for Fake {
    async fn operate(&self, _: &Workload, _: Action) -> Result<()> {
        Ok(())
    }
    async fn observe(&self, _: &Workload) -> Result<Observation> {
        Ok(Observation {
            state: "inactive".into(),
            ..Default::default()
        })
    }
    async fn scan(&self, _: devdash::model::Scope) -> Result<Vec<String>> {
        Ok(vec![
            "imported.service".into(),
            "devdash-demo-camera.service".into(),
        ])
    }
}
#[tokio::test]
async fn clients_disconnect_without_losing_gate_and_import_is_explicit() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let socket = dir.path().join("core.sock");
    let yaml = dir.path().join("config.yaml");
    std::fs::write(&yaml, include_str!("../examples/robot.yaml"))?;
    let e = Engine::new(Config::load(&yaml)?, Arc::new(Fake), None, "boot".into())?;
    let server = {
        let e = e.clone();
        let socket = socket.clone();
        let yaml = yaml.clone();
        tokio::spawn(async move { ipc::serve(e, &socket, yaml).await })
    };
    tokio::time::timeout(Duration::from_secs(2), async {
        while !socket.exists() {
            tokio::task::yield_now().await;
        }
    })
    .await?;
    let before = std::fs::read(&yaml)?;
    ipc::request(
        &socket,
        Request::Scan {
            scope: devdash::model::Scope::User,
        },
    )
    .await?;
    assert_eq!(before, std::fs::read(&yaml)?);
    ipc::request(
        &socket,
        Request::Operate {
            target: "robot".into(),
            group: true,
            action: Action::Start,
        },
    )
    .await?;
    tokio::time::timeout(Duration::from_secs(2), async {
        while e.snapshot().waiting_gate.is_none() {
            tokio::task::yield_now().await;
        }
    })
    .await?;
    let snap: Snapshot =
        serde_json::from_value(ipc::request(&socket, Request::Snapshot).await?.data)?;
    assert_eq!(snap.run.waiting_gate.as_deref(), Some("sensors_ready"));
    assert!(
        ipc::request(
            &socket,
            Request::Operate {
                target: "camera".into(),
                group: false,
                action: Action::Start
            }
        )
        .await
        .is_err()
    );
    ipc::request(
        &socket,
        Request::Approve {
            gate: "sensors_ready".into(),
        },
    )
    .await?;
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(2), e.wait())
            .await?
            .status,
        "completed"
    );
    ipc::request(
        &socket,
        Request::Import {
            scope: devdash::model::Scope::User,
            unit: "imported.service".into(),
        },
    )
    .await?;
    assert!(Config::load(&yaml)?.workloads.contains_key("imported"));
    assert!(yaml.with_extension("yaml.bak").exists());
    assert!(
        ipc::request(
            &socket,
            Request::Import {
                scope: devdash::model::Scope::User,
                unit: "unknown.service".into()
            }
        )
        .await
        .is_err()
    );
    // A second daemon cannot remove the first one's live socket.
    assert!(ipc::serve(e.clone(), &socket, yaml.clone()).await.is_err());
    assert!(ipc::request(&socket, Request::Snapshot).await.is_ok());
    server.abort();
    let _ = server.await;
    assert!(!socket.exists());
    // An abandoned socket from a crashed daemon can be reclaimed under the lock.
    let abandoned = std::os::unix::net::UnixListener::bind(&socket)?;
    drop(abandoned);
    let server = {
        let socket = socket.clone();
        tokio::spawn(async move { ipc::serve(e, &socket, yaml).await })
    };
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            if ipc::request(&socket, Request::Snapshot).await.is_ok() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await?;
    server.abort();
    let _ = server.await;
    Ok(())
}
