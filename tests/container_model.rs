//! Core tests need neither Docker, a ROS installation nor a real supervisor.
use anyhow::Result;
use async_trait::async_trait;
use devdash::{
    engine::{Action, Backend, Engine, Observation},
    model::*,
};
use std::{
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};
use tokio::sync::{Notify, Semaphore};
use tokio_util::sync::CancellationToken;
const CONFIG: &str = r#"
workloads:
  robot:
    backend: docker
    docker: {container: robot-dev}
groups:
  robot:
    steps: [{workload: robot}, {gate: ready}]
gates:
  ready: {type: container_ready, workload: robot, timeout_ms: 40, interval_ms: 5}
"#;
struct Fake {
    revision: AtomicU64,
    health: Mutex<Option<String>>,
    calls: Mutex<Vec<String>>,
    entered: Notify,
    release: Semaphore,
}
impl Fake {
    fn new(health: Option<&str>) -> Arc<Self> {
        Arc::new(Self {
            revision: AtomicU64::new(0),
            health: Mutex::new(health.map(str::to_owned)),
            calls: Mutex::new(vec![]),
            entered: Notify::new(),
            release: Semaphore::new(0),
        })
    }
}
#[async_trait]
impl Backend for Fake {
    async fn operate(&self, _w: &Workload, a: Action) -> Result<()> {
        self.calls.lock().unwrap().push(format!("parent:{a:?}"));
        Ok(())
    }
    async fn observe(&self, _w: &Workload) -> Result<Observation> {
        Ok(Observation {
            state: "active".into(),
            health: self.health.lock().unwrap().clone(),
            ..Default::default()
        })
    }
    async fn scan(&self, _scope: Scope) -> Result<Vec<String>> {
        Ok(vec![])
    }
    async fn operate_child(&self, _w: &Workload, id: &str, _epoch: &str, a: Action) -> Result<()> {
        self.calls.lock().unwrap().push(format!("{id}:{a:?}"));
        self.entered.notify_one();
        self.release.acquire().await?.forget();
        Ok(())
    }
    fn inspection_revision(&self) -> u64 {
        self.revision.load(Ordering::SeqCst)
    }
    fn operation_loss(&self, _scope: Scope) -> Option<CancellationToken> {
        let token = CancellationToken::new();
        token.cancel();
        Some(token)
    }
}
#[test]
fn container_reference_validation_and_children_cannot_be_steps() {
    let config = Config::parse(CONFIG).unwrap();
    assert!(config.workloads["robot"].systemd_key().is_none());
    for invalid in [
        CONFIG.replace("{container: robot-dev}", "{}"),
        CONFIG.replace(
            "{container: robot-dev}",
            "{container: robot-dev, compose: {project: robot, service: camera}}",
        ),
        CONFIG.replace("{workload: robot}", "{workload: worker:worker}"),
        CONFIG.replace(
            "workload: robot, timeout_ms",
            "workload: missing, timeout_ms",
        ),
    ] {
        assert!(Config::parse(&invalid).is_err(), "{invalid}");
    }
}
#[tokio::test]
async fn container_gate_delegates_health_and_is_independent_of_systemd_manager() {
    for health in [None, Some("healthy")] {
        let fake = Fake::new(health);
        let e = Engine::new(
            Config::parse(CONFIG).unwrap(),
            fake.clone(),
            None,
            "test".into(),
        )
        .unwrap();
        let report = e.inspect("robot", Action::Start).await.unwrap();
        assert!(report.requested_units.is_empty());
        assert!(
            report
                .incomplete
                .iter()
                .any(|s| s.contains("Docker lifecycle"))
        );
        fake.revision.fetch_add(1, Ordering::SeqCst);
        assert!(
            !e.reports()["robot"].stale,
            "unrelated systemd manager revisions must not invalidate Docker-only diagnostics"
        );
        e.launch("robot".into(), true, Action::Start).unwrap();
        assert_eq!(e.wait().await.status, "completed");
    }
    let fake = Fake::new(Some("unhealthy"));
    let e = Engine::new(
        Config::parse(CONFIG).unwrap(),
        fake.clone(),
        None,
        "test".into(),
    )
    .unwrap();
    e.launch("robot".into(), true, Action::Start).unwrap();
    let failed = e.wait().await;
    assert_eq!(failed.status, "failed");
    assert!(failed.progress.iter().any(|p| {
        p.error
            .as_ref()
            .is_some_and(|e| e.contains("readiness gate"))
    }));
    assert_eq!(
        fake.calls.lock().unwrap().len(),
        1,
        "no rollback after unhealthy timeout"
    );
    e.launch("robot".into(), false, Action::Start).unwrap();
    assert_eq!(
        e.wait().await.status,
        "completed",
        "individual Start bypasses readiness"
    );
}
#[tokio::test]
async fn child_operation_uses_global_busy_and_cancel_tracks_issued_operation() {
    let fake = Fake::new(Some("healthy"));
    let e = Engine::new(
        Config::parse(CONFIG).unwrap(),
        fake.clone(),
        None,
        "test".into(),
    )
    .unwrap();
    e.launch_child(
        "robot".into(),
        "worker:worker".into(),
        "epoch".into(),
        Action::Restart,
    )
    .unwrap();
    tokio::time::timeout(Duration::from_secs(1), fake.entered.notified())
        .await
        .unwrap();
    assert!(e.launch("robot".into(), true, Action::Stop).is_err());
    assert!(
        e.launch_child("robot".into(), "other".into(), "epoch".into(), Action::Stop)
            .is_err()
    );
    e.cancel().unwrap();
    assert_eq!(e.snapshot().status, "cancelling");
    fake.release.add_permits(1);
    let cancelled = e.wait().await;
    assert_eq!(cancelled.status, "cancelled");
    assert_eq!(cancelled.progress[0].status, "completed");
    assert_eq!(*fake.calls.lock().unwrap(), vec!["worker:worker:Restart"]);
    assert!(
        e.launch_child(
            "robot".into(),
            "worker".into(),
            "epoch".into(),
            Action::Reinitialize
        )
        .is_err()
    );
}
#[tokio::test]
async fn missing_docker_socket_reports_access_context() {
    let dir = tempfile::tempdir().unwrap();
    let mut c = Config::parse(CONFIG).unwrap();
    c.workloads
        .get_mut("robot")
        .unwrap()
        .docker
        .as_mut()
        .unwrap()
        .socket = dir.path().join("missing.sock");
    let error = devdash::docker::DockerApi::connect(c.workloads["robot"].docker.as_ref().unwrap())
        .await
        .err()
        .unwrap();
    assert!(format!("{error:#}").contains("Docker socket"));
}
