use anyhow::{Result, bail};
use async_trait::async_trait;
use devdash::{engine::*, model::*};
use std::{
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::sync::Semaphore;
#[derive(Default)]
struct Fake {
    calls: Mutex<Vec<(String, Action)>>,
    fail: Mutex<Option<String>>,
    delay: Mutex<Option<Arc<Semaphore>>>,
}
#[async_trait]
impl Backend for Fake {
    async fn operate(&self, w: &Workload, a: Action) -> Result<()> {
        self.calls.lock().unwrap().push((w.unit.clone(), a));
        let delay = self.delay.lock().unwrap().clone();
        if let Some(s) = delay {
            s.acquire().await?.forget();
        }
        if self.fail.lock().unwrap().as_ref() == Some(&w.unit) {
            bail!("fake job failure");
        }
        Ok(())
    }
    async fn observe(&self, _: &Workload) -> Result<Observation> {
        Ok(Observation::default())
    }
    async fn scan(&self, _: devdash::model::Scope) -> Result<Vec<String>> {
        Ok(vec![])
    }
}
fn config() -> Config {
    Config::parse(include_str!("../examples/robot.yaml")).unwrap()
}
fn setup() -> (Arc<Engine>, Arc<Fake>) {
    let f = Arc::new(Fake::default());
    let e = Engine::new(config(), f.clone(), None, "boot".into()).unwrap();
    (e, f)
}
async fn gate(e: &Engine) {
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            if e.snapshot().waiting_gate.is_some() {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
}
async fn finished(e: &Engine) -> Run {
    tokio::time::timeout(Duration::from_secs(3), e.wait())
        .await
        .unwrap()
}
fn calls(f: &Fake) -> Vec<(String, Action)> {
    f.calls.lock().unwrap().clone()
}
#[tokio::test]
async fn hierarchy_order_reverse_restart_and_scope() {
    let (e, f) = setup();
    e.approve("sensors_ready").unwrap();
    e.launch("robot".into(), true, Action::Start).unwrap();
    assert_eq!(finished(&e).await.status, "completed");
    let c = calls(&f);
    assert_eq!(c.len(), 4);
    assert_eq!(c[2].0, "devdash-demo-detector.service");
    assert_eq!(c[3].0, "devdash-demo-planner.service");
    f.calls.lock().unwrap().clear();
    e.launch("robot".into(), true, Action::Stop).unwrap();
    finished(&e).await;
    let c = calls(&f);
    assert_eq!(c[0], ("devdash-demo-planner.service".into(), Action::Stop));
    assert_eq!(c[1].0, "devdash-demo-detector.service");
    f.calls.lock().unwrap().clear();
    e.launch("perception".into(), true, Action::Restart)
        .unwrap();
    finished(&e).await;
    assert_eq!(
        calls(&f),
        vec![
            ("devdash-demo-detector.service".into(), Action::Stop),
            ("devdash-demo-detector.service".into(), Action::Start)
        ]
    );
    f.calls.lock().unwrap().clear();
    e.launch("robot".into(), true, Action::Restart).unwrap();
    assert_eq!(finished(&e).await.status, "completed");
    assert_eq!(calls(&f).len(), 8);
    assert_eq!(calls(&f)[0].1, Action::Stop);
    assert_eq!(calls(&f)[4].1, Action::Start);
}
#[tokio::test]
async fn manual_wait_approve_and_direct_bypass() {
    let (e, f) = setup();
    e.launch("detector".into(), false, Action::Start).unwrap();
    finished(&e).await;
    assert_eq!(calls(&f).len(), 1);
    f.calls.lock().unwrap().clear();
    e.launch("robot".into(), true, Action::Start).unwrap();
    gate(&e).await;
    assert_eq!(calls(&f).len(), 2);
    assert!(e.launch("planner".into(), false, Action::Start).is_err());
    e.approve("sensors_ready").unwrap();
    assert_eq!(finished(&e).await.status, "completed");
    assert_eq!(calls(&f).len(), 4);
    e.launch("robot".into(), true, Action::Restart).unwrap();
    assert_eq!(finished(&e).await.status, "completed");
    e.launch("robot".into(), true, Action::Reinitialize)
        .unwrap();
    gate(&e).await;
    assert!(!e.approved().contains("sensors_ready"));
    e.cancel().unwrap();
    assert_eq!(finished(&e).await.status, "cancelled");
}
#[tokio::test]
async fn cancel_gate_and_skip_following_no_rollback() {
    let (e, f) = setup();
    e.launch("robot".into(), true, Action::Start).unwrap();
    gate(&e).await;
    e.cancel().unwrap();
    let r = finished(&e).await;
    assert_eq!(r.status, "cancelled");
    assert_eq!(calls(&f).len(), 2);
    assert!(
        r.progress
            .iter()
            .any(|p| p.step == "detector" && p.status == "skipped")
    );
}
#[tokio::test]
async fn parallel_cancel_drains_issued_jobs() {
    let (e, f) = setup();
    let block = Arc::new(Semaphore::new(0));
    *f.delay.lock().unwrap() = Some(block.clone());
    e.launch("robot".into(), true, Action::Start).unwrap();
    tokio::time::timeout(Duration::from_secs(2), async {
        while calls(&f).len() != 2 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    e.cancel().unwrap();
    assert_eq!(e.snapshot().status, "cancelling");
    assert!(e.launch("planner".into(), false, Action::Start).is_err());
    block.add_permits(2);
    assert_eq!(finished(&e).await.status, "cancelled");
    assert_eq!(calls(&f).len(), 2);
}
#[tokio::test]
async fn restart_cancel_during_stop_never_starts() {
    let (e, f) = setup();
    let block = Arc::new(Semaphore::new(0));
    *f.delay.lock().unwrap() = Some(block.clone());
    e.launch("robot".into(), true, Action::Restart).unwrap();
    while calls(&f).is_empty() {
        tokio::task::yield_now().await;
    }
    e.cancel().unwrap();
    block.add_permits(1);
    finished(&e).await;
    assert_eq!(
        calls(&f),
        vec![("devdash-demo-planner.service".into(), Action::Stop)]
    );
}
#[tokio::test]
async fn failure_drains_parallel_and_no_rollback() {
    let (e, f) = setup();
    *f.fail.lock().unwrap() = Some("devdash-demo-camera.service".into());
    e.launch("robot".into(), true, Action::Start).unwrap();
    let r = finished(&e).await;
    assert_eq!(r.status, "failed");
    assert_eq!(calls(&f).len(), 2);
    assert!(calls(&f).iter().all(|c| c.1 == Action::Start));
    assert!(
        r.progress
            .iter()
            .any(|p| p.step == "planner" && p.status == "skipped")
    );
}
#[tokio::test]
async fn persistence_boot_reset_and_corruption() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("state.json");
    let f = Arc::new(Fake::default());
    let e = Engine::new(config(), f.clone(), Some(path.clone()), "boot-a".into()).unwrap();
    e.approve("sensors_ready").unwrap();
    drop(e);
    let e = Engine::new(config(), f.clone(), Some(path.clone()), "boot-a".into()).unwrap();
    assert!(e.approved().contains("sensors_ready"));
    drop(e);
    let e = Engine::new(config(), f.clone(), Some(path.clone()), "boot-b".into()).unwrap();
    assert!(e.approved().is_empty());
    drop(e);
    std::fs::write(&path, "broken").unwrap();
    assert!(Engine::new(config(), f, Some(path), "boot-b".into()).is_err());
}
#[test]
fn rejects_invalid_references_cycles_duplicates_and_steps() {
    let mut c = config();
    c.groups
        .get_mut("planning")
        .unwrap()
        .steps
        .push(Step::Group("robot".into()));
    assert!(c.validate().is_err());
    let mut c = config();
    c.groups
        .get_mut("planning")
        .unwrap()
        .steps
        .push(Step::Workload("camera".into()));
    assert!(c.validate().is_err());
    let mut c = config();
    c.groups
        .get_mut("planning")
        .unwrap()
        .steps
        .push(Step::Group("sensors".into()));
    assert!(c.validate().is_err());
    let mut c = config();
    c.groups
        .get_mut("planning")
        .unwrap()
        .steps
        .push(Step::Gate("missing".into()));
    assert!(c.validate().is_err());
    let mut c = config();
    c.groups
        .get_mut("planning")
        .unwrap()
        .steps
        .push(Step::Parallel(vec![]));
    assert!(c.validate().is_err());
    let bad = include_str!("../examples/robot.yaml").replace(
        "- workload: planner",
        "- workload: planner\n        group: sensors",
    );
    assert!(Config::parse(&bad).is_err());
    let bad = include_str!("../examples/robot.yaml").replace("backend: systemd, ", "");
    assert!(Config::parse(&bad).is_err());
    let c = config();
    assert!(Config::parse(&serde_yaml::to_string(&c).unwrap()).is_ok());
}
async fn http_engine(status: u16) -> (Arc<Engine>, Arc<Fake>, tokio::task::JoinHandle<()>) {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let task = tokio::spawn(async move {
        loop {
            let (mut s, _) = listener.accept().await.unwrap();
            tokio::spawn(async move {
                let mut b = [0; 2048];
                let _ = s.read(&mut b).await;
                let _=s.write_all(format!("HTTP/1.1 {status} Test\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").as_bytes()).await;
            });
        }
    });
    let mut c = config();
    c.gates.insert(
        "sensors_ready".into(),
        Gate::Http {
            url: format!("http://{addr}"),
            timeout_ms: 150,
            interval_ms: 10,
            expected_status: 200,
        },
    );
    let f = Arc::new(Fake::default());
    (
        Engine::new(c, f.clone(), None, "boot".into()).unwrap(),
        f,
        task,
    )
}
#[tokio::test]
async fn http_success_and_reevaluation() {
    let (e, f, t) = http_engine(200).await;
    e.launch("robot".into(), true, Action::Start).unwrap();
    assert_eq!(finished(&e).await.status, "completed");
    t.abort();
    f.calls.lock().unwrap().clear();
    e.launch("robot".into(), true, Action::Start).unwrap();
    assert_eq!(finished(&e).await.status, "failed");
    assert_eq!(calls(&f).len(), 2);
}
#[tokio::test]
async fn http_timeout() {
    let (e, f, t) = http_engine(503).await;
    e.launch("robot".into(), true, Action::Start).unwrap();
    assert_eq!(finished(&e).await.status, "failed");
    assert_eq!(calls(&f).len(), 2);
    t.abort();
}
#[tokio::test]
async fn http_cancel() {
    let (e, _, t) = http_engine(503).await;
    e.launch("robot".into(), true, Action::Start).unwrap();
    gate(&e).await;
    e.cancel().unwrap();
    assert_eq!(finished(&e).await.status, "cancelled");
    t.abort();
}
#[tokio::test]
async fn direct_restart_uses_backend_restart() {
    let (e, f) = setup();
    e.launch("detector".into(), false, Action::Restart).unwrap();
    finished(&e).await;
    assert_eq!(calls(&f)[0].1, Action::Restart);
}

#[tokio::test]
async fn immediate_cancel_does_not_issue_individual_operation() {
    let (e, f) = setup();
    e.launch("camera".into(), false, Action::Start).unwrap();
    e.cancel().unwrap();
    assert_eq!(finished(&e).await.status, "cancelled");
    assert!(calls(&f).is_empty());
    assert!(e.snapshot().progress.iter().all(|p| p.status == "skipped"));
}
#[tokio::test]
async fn stop_failure_prevents_restart_start_phase() {
    let (e, f) = setup();
    *f.fail.lock().unwrap() = Some("devdash-demo-planner.service".into());
    e.launch("robot".into(), true, Action::Restart).unwrap();
    assert_eq!(finished(&e).await.status, "failed");
    assert_eq!(
        calls(&f),
        vec![("devdash-demo-planner.service".into(), Action::Stop)]
    );
}
#[tokio::test]
async fn reinitialize_resets_only_selected_group_gates() {
    let mut c = config();
    c.gates.insert("independent_gate".into(), Gate::Manual);
    c.groups.insert(
        "independent".into(),
        Group {
            name: String::new(),
            steps: vec![Step::Gate("independent_gate".into())],
        },
    );
    let e = Engine::new(c, Arc::new(Fake::default()), None, "boot".into()).unwrap();
    e.approve("sensors_ready").unwrap();
    e.approve("independent_gate").unwrap();
    e.launch("robot".into(), true, Action::Reinitialize)
        .unwrap();
    gate(&e).await;
    assert!(e.approved().contains("independent_gate"));
    assert!(!e.approved().contains("sensors_ready"));
    e.cancel().unwrap();
    finished(&e).await;
}
#[tokio::test]
async fn system_scope_operations_reach_backend() {
    let mut c = config();
    c.workloads.get_mut("camera").unwrap().scope = Scope::System;
    let f = Arc::new(Fake::default());
    let e = Engine::new(c, f.clone(), None, "boot".into()).unwrap();
    e.launch("camera".into(), false, Action::Start).unwrap();
    assert_eq!(finished(&e).await.status, "completed");
    assert_eq!(calls(&f).len(), 1);
}
#[tokio::test]
async fn restart_cancel_in_start_phase_skips_remainder() {
    let (e, f) = setup();
    e.approve("sensors_ready").unwrap();
    let permits = Arc::new(Semaphore::new(4));
    *f.delay.lock().unwrap() = Some(permits.clone());
    e.launch("robot".into(), true, Action::Restart).unwrap();
    tokio::time::timeout(Duration::from_secs(2), async {
        while calls(&f).len() < 6 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    e.cancel().unwrap();
    permits.add_permits(2);
    assert_eq!(finished(&e).await.status, "cancelled");
    let c = calls(&f);
    assert_eq!(c.len(), 6);
    assert!(c[..4].iter().all(|c| c.1 == Action::Stop));
    assert!(c[4..].iter().all(|c| c.1 == Action::Start));
}

#[tokio::test]
async fn progress_tracks_steps_and_parallel_members_once() {
    let (e, _) = setup();
    e.launch("robot".into(), true, Action::Start).unwrap();
    gate(&e).await;
    let waiting = e.snapshot();
    for id in ["camera", "imu", "parallel: camera, imu"] {
        let entries: Vec<_> = waiting.progress.iter().filter(|p| p.step == id).collect();
        assert_eq!(entries.len(), 1, "duplicate progress for {id}");
        assert_eq!(entries[0].status, "completed");
    }
    assert_eq!(waiting.progress.last().unwrap().step, "sensors_ready");
    assert_eq!(waiting.progress.last().unwrap().status, "running");
    assert!(!waiting.progress.iter().any(|p| p.step == "detector"));
    e.approve("sensors_ready").unwrap();
    let completed = finished(&e).await;
    assert_eq!(completed.progress.len(), 6);
    assert!(completed.progress.iter().all(|p| p.status == "completed"));
}

#[tokio::test]
async fn issued_job_failure_is_preserved_after_cancel() {
    let (e, f) = setup();
    let block = Arc::new(Semaphore::new(0));
    *f.delay.lock().unwrap() = Some(block.clone());
    *f.fail.lock().unwrap() = Some("devdash-demo-camera.service".into());
    e.launch("robot".into(), true, Action::Start).unwrap();
    tokio::time::timeout(Duration::from_secs(2), async {
        while calls(&f).len() != 2 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    e.cancel().unwrap();
    assert_eq!(e.snapshot().status, "cancelling");
    block.add_permits(2);
    let run = finished(&e).await;
    assert_eq!(run.status, "failed");
    for (id, expected) in [
        ("camera", "failed"),
        ("imu", "completed"),
        ("detector", "skipped"),
    ] {
        assert_eq!(
            run.progress.iter().find(|p| p.step == id).unwrap().status,
            expected
        );
    }
    assert_eq!(calls(&f).len(), 2);
    assert!(run.progress.iter().all(|p| p.status != "running"));
}
