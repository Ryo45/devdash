use anyhow::Result;
use async_trait::async_trait;
use devdash::{
    engine::{Action, Backend, Engine, Observation},
    inspection::*,
    model::*,
    monitor::Monitor,
};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::{Arc, Mutex},
    time::Duration,
};
fn config() -> Config {
    Config::parse(include_str!("../examples/robot.yaml")).unwrap()
}
fn key(id: &str) -> UnitKey {
    UnitKey::from(&config().workloads[id])
}
fn graph() -> DependencyGraph {
    DependencyGraph {
        inspected: config().workloads.values().map(UnitKey::from).collect(),
        ..Default::default()
    }
}
fn check(g: &DependencyGraph, group: &str, action: Action) -> InspectionReport {
    inspect(&config(), group, action, &BTreeSet::new(), g).unwrap()
}
#[test]
fn wants_and_requires_bypass_gate_with_different_certainty() {
    for (relation, level) in [
        (Relation::Wants, "warning"),
        (Relation::Requires, "error"),
        (Relation::BindsTo, "error"),
    ] {
        let mut g = graph();
        g.edge(key("camera"), key("detector"), relation);
        let r = check(&g, "robot", Action::Start);
        assert!(
            r.diagnostics
                .iter()
                .any(|d| d.code == "gate_bypass" && d.level == level)
        );
        assert_eq!(r.blocked(), level == "error");
    }
}
#[test]
fn unregistered_intermediate_and_inverse_stop_propagation() {
    let mut g = graph();
    let bridge = UnitKey::new(Scope::User, "bridge.target");
    g.edge(key("camera"), bridge.clone(), Relation::Requires);
    g.edge(bridge, key("detector"), Relation::Requires);
    assert!(check(&g, "robot", Action::Start).blocked());
    for relation in [
        Relation::PartOf,
        Relation::BindsTo,
        Relation::Requires,
        Relation::Requisite,
    ] {
        let mut g = graph();
        g.edge(key("planner"), key("detector"), relation);
        let r = check(&g, "perception", Action::Restart);
        assert!(
            r.diagnostics
                .iter()
                .any(|d| d.code == "outside_stop" && d.units.contains(&key("planner")))
        );
        assert!(
            !check(&g, "planning", Action::Stop)
                .diagnostics
                .iter()
                .any(|d| d.code == "outside_stop")
        );
    }
}
#[test]
fn ordering_does_not_create_start_requests() {
    let mut g = graph();
    g.edge(key("camera"), key("detector"), Relation::After);
    let r = check(&g, "robot", Action::Start);
    assert!(!r.blocked());
    assert!(r.diagnostics.iter().any(|d| d.code == "order_mismatch"));
    assert!(!r.diagnostics.iter().any(|d| d.code == "gate_bypass"));
}
#[test]
fn before_conflicts_and_stop_to_are_distinct() {
    let mut g = graph();
    g.edge(key("detector"), key("camera"), Relation::Before);
    assert!(
        check(&g, "robot", Action::Start)
            .diagnostics
            .iter()
            .any(|d| d.code == "order_mismatch")
    );
    g.edge(key("camera"), key("detector"), Relation::Conflicts);
    assert!(check(&g, "robot", Action::Start).blocked());
    assert!(!check(&g, "robot", Action::Stop).blocked());
    g.edge(key("detector"), key("planner"), Relation::PropagatesStopTo);
    assert!(
        check(&g, "perception", Action::Stop)
            .diagnostics
            .iter()
            .any(|d| d.code == "outside_stop")
    );
}
#[test]
fn approval_reuse_and_reinitialize_change_gate_analysis() {
    let mut g = graph();
    g.edge(key("camera"), key("detector"), Relation::Requires);
    let approved = BTreeSet::from(["sensors_ready".into()]);
    assert!(
        !inspect(&config(), "robot", Action::Restart, &approved, &g)
            .unwrap()
            .blocked()
    );
    assert!(
        inspect(&config(), "robot", Action::Reinitialize, &approved, &g)
            .unwrap()
            .blocked()
    );
}
#[test]
fn same_name_different_managers_and_import() {
    let mut c = config();
    let unit = c.workloads["camera"].unit.clone();
    let id = c.import(&unit, Scope::System).unwrap();
    assert_ne!(id, "camera");
    assert_eq!(c.workloads[&id].scope, Scope::System);
    c.validate().unwrap();
    let mut g = graph();
    g.edge(
        UnitKey::new(Scope::System, &unit),
        key("detector"),
        Relation::Requires,
    );
    assert!(
        !inspect(&c, "robot", Action::Start, &BTreeSet::new(), &g)
            .unwrap()
            .blocked()
    );
    let mut c = Config {
        workloads: BTreeMap::new(),
        groups: BTreeMap::new(),
        gates: BTreeMap::new(),
    };
    let first = c.import("same.service", Scope::User).unwrap();
    let second = c.import("same.service", Scope::System).unwrap();
    assert_ne!(first, second);
    c.validate().unwrap();
}
#[test]
fn partial_inspection_is_explicit_and_cycles_terminate() {
    let mut g = graph();
    g.edge(key("camera"), key("imu"), Relation::Requires);
    g.edge(key("imu"), key("camera"), Relation::Requires);
    g.incomplete.push("permission denied".into());
    let r = check(&g, "robot", Action::Start);
    assert_eq!(r.incomplete, vec!["permission denied"]);
    assert!(!r.blocked());
}
#[derive(Default)]
struct Fake {
    graph: Mutex<DependencyGraph>,
    calls: Mutex<Vec<(Scope, String)>>,
    paused: std::sync::atomic::AtomicBool,
    entered: tokio::sync::Notify,
    resume: tokio::sync::Notify,
    seeds: Mutex<Vec<UnitKey>>,
}
#[async_trait]
impl Backend for Fake {
    async fn operate(&self, w: &Workload, _: Action) -> Result<()> {
        self.calls.lock().unwrap().push((w.scope, w.unit.clone()));
        Ok(())
    }
    async fn observe(&self, _: &Workload) -> Result<Observation> {
        Ok(Observation::default())
    }
    async fn scan(&self, _: Scope) -> Result<Vec<String>> {
        Ok(vec![])
    }
    async fn dependencies(&self, seeds: &[UnitKey]) -> Result<DependencyGraph> {
        *self.seeds.lock().unwrap() = seeds.to_vec();
        let snapshot = self.graph.lock().unwrap().clone();
        if self.paused.load(std::sync::atomic::Ordering::SeqCst) {
            self.entered.notify_one();
            self.resume.notified().await;
        }
        Ok(snapshot)
    }
}
#[tokio::test]
async fn configuration_is_reinspected_before_every_operation_and_blocked_before_jobs() {
    let f = Arc::new(Fake::default());
    let e = Engine::new(config(), f.clone(), None, "boot".into()).unwrap();
    assert!(!e.inspect("robot", Action::Start).await.unwrap().blocked());
    f.graph
        .lock()
        .unwrap()
        .edge(key("camera"), key("detector"), Relation::Requires);
    e.launch("robot".into(), true, Action::Start).unwrap();
    let r = tokio::time::timeout(Duration::from_secs(2), e.wait())
        .await
        .unwrap();
    assert_eq!(r.status, "failed");
    assert!(f.calls.lock().unwrap().is_empty());
    f.graph.lock().unwrap().edges.clear();
    e.launch("perception".into(), true, Action::Start).unwrap();
    assert_eq!(e.wait().await.status, "completed");
    assert_eq!(f.calls.lock().unwrap().len(), 1);
}
fn observations(state: &str) -> BTreeMap<String, Observation> {
    BTreeMap::from([(
        "detector".into(),
        Observation {
            state: state.into(),
            ..Default::default()
        },
    )])
}
#[test]
fn observation_baseline_unattributed_changes_and_no_repeat_notifications() {
    let c = config();
    let mut m = Monitor::default();
    let approved = BTreeSet::from(["sensors_ready".into()]);
    m.update(
        &c,
        &Default::default(),
        &approved,
        observations("inactive"),
        &BTreeSet::new(),
    );
    assert!(m.events().is_empty());
    m.update(
        &c,
        &Default::default(),
        &approved,
        observations("active"),
        &BTreeSet::new(),
    );
    assert_eq!(m.events().len(), 1);
    assert!(m.events()[0].message.contains("origin unknown"));
    m.update(
        &c,
        &Default::default(),
        &approved,
        observations("active"),
        &BTreeSet::new(),
    );
    assert_eq!(m.events().len(), 1);
    m.update(
        &c,
        &Default::default(),
        &approved,
        observations("not_found"),
        &BTreeSet::new(),
    );
    assert_eq!(m.events().last().unwrap().level, "warning");
    m.update(
        &c,
        &Default::default(),
        &approved,
        observations("inactive"),
        &BTreeSet::new(),
    );
    assert!(
        m.events()
            .last()
            .unwrap()
            .message
            .contains("not_found → inactive")
    );
}
#[test]
fn gate_mismatch_and_unexpected_change_during_operation_warn_once() {
    let c = config();
    let mut m = Monitor::default();
    m.update(
        &c,
        &Default::default(),
        &BTreeSet::new(),
        observations("active"),
        &BTreeSet::new(),
    );
    assert_eq!(m.events().len(), 1);
    assert!(m.events()[0].message.contains("gate sensors_ready"));
    m.update(
        &c,
        &Default::default(),
        &BTreeSet::new(),
        observations("active"),
        &BTreeSet::new(),
    );
    assert_eq!(m.events().len(), 1);
    let run = devdash::engine::Run {
        status: "running".into(),
        ..Default::default()
    };
    m.update(
        &c,
        &run,
        &BTreeSet::new(),
        observations("inactive"),
        &BTreeSet::new(),
    );
    assert_eq!(m.events().last().unwrap().level, "warning");
}
#[test]
fn invocation_restart_and_bounded_log() {
    let c = config();
    let mut m = Monitor::default();
    let approved = BTreeSet::from(["sensors_ready".into()]);
    let mut a = observations("active");
    a.get_mut("detector").unwrap().invocation_id = Some("first".into());
    m.update(
        &c,
        &Default::default(),
        &approved,
        a.clone(),
        &BTreeSet::new(),
    );
    a.get_mut("detector").unwrap().invocation_id = Some("next".into());
    m.update(&c, &Default::default(), &approved, a, &BTreeSet::new());
    assert!(m.events()[0].message.contains("invocation changed"));
    for i in 0..300 {
        m.record("info", None, i.to_string());
    }
    assert_eq!(m.events().len(), 200);
}

#[derive(Default)]
struct PausedInspection {
    entered: tokio::sync::Notify,
    resume: tokio::sync::Notify,
    calls: std::sync::atomic::AtomicUsize,
}
#[async_trait]
impl Backend for PausedInspection {
    async fn operate(&self, _: &Workload, _: Action) -> Result<()> {
        self.calls.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        Ok(())
    }
    async fn observe(&self, _: &Workload) -> Result<Observation> {
        Ok(Observation::default())
    }
    async fn scan(&self, _: Scope) -> Result<Vec<String>> {
        Ok(vec![])
    }
    async fn dependencies(&self, _: &[UnitKey]) -> Result<DependencyGraph> {
        self.entered.notify_one();
        self.resume.notified().await;
        Ok(DependencyGraph::default())
    }
}
#[tokio::test]
async fn cancel_during_preflight_preserves_approvals_and_issues_no_jobs() {
    let backend = Arc::new(PausedInspection::default());
    let engine = Engine::new(config(), backend.clone(), None, "boot".into()).unwrap();
    engine.approve("sensors_ready").unwrap();
    engine
        .launch("robot".into(), true, Action::Reinitialize)
        .unwrap();
    tokio::time::timeout(Duration::from_secs(2), backend.entered.notified())
        .await
        .unwrap();
    engine.cancel().unwrap();
    backend.resume.notify_one();
    let run = tokio::time::timeout(Duration::from_secs(2), engine.wait())
        .await
        .unwrap();
    assert_eq!(run.status, "cancelled");
    assert!(engine.approved().contains("sensors_ready"));
    assert_eq!(backend.calls.load(std::sync::atomic::Ordering::SeqCst), 0);
}

#[test]
fn partial_group_warns_about_outside_start_without_enforcing_parent_gate() {
    let mut g = graph();
    g.edge(key("camera"), key("detector"), Relation::Requires);
    let report = check(&g, "sensors", Action::Start);
    assert!(!report.blocked());
    let outside = report
        .diagnostics
        .iter()
        .find(|d| d.code == "outside_start")
        .unwrap();
    assert!(outside.units.contains(&key("detector")));
    assert!(outside.message.contains("sensors_ready"));
    assert!(check(&g, "robot", Action::Start).blocked());
}
#[test]
fn observation_outage_does_not_clear_readiness_episode_or_infer_restart() {
    let c = config();
    let mut monitor = Monitor::default();
    for state in ["active", "unknown", "active", "unknown", "active"] {
        monitor.update(
            &c,
            &Default::default(),
            &BTreeSet::new(),
            observations(state),
            &BTreeSet::new(),
        );
    }
    let events = monitor.events();
    assert_eq!(
        events
            .iter()
            .filter(|e| e.message.contains("Manual gate"))
            .count(),
        1
    );
    assert_eq!(
        events
            .iter()
            .filter(|e| e.kind == devdash::monitor::EventKind::ObservationLost)
            .count(),
        2
    );
    assert!(
        !events
            .iter()
            .any(|e| e.kind == devdash::monitor::EventKind::StateChanged)
    );
    monitor.update(
        &c,
        &Default::default(),
        &BTreeSet::new(),
        observations("inactive"),
        &BTreeSet::new(),
    );
    monitor.update(
        &c,
        &Default::default(),
        &BTreeSet::new(),
        observations("active"),
        &BTreeSet::new(),
    );
    assert_eq!(
        monitor
            .events()
            .iter()
            .filter(|e| e.message.contains("Manual gate"))
            .count(),
        2
    );
}
#[tokio::test]
async fn invalidated_refresh_cannot_publish_old_snapshot_as_fresh() {
    use std::sync::atomic::Ordering;
    let backend = Arc::new(Fake::default());
    let e = Engine::new(config(), backend.clone(), None, "boot".into()).unwrap();
    e.inspect("robot", Action::Start).await.unwrap();
    backend.paused.store(true, Ordering::SeqCst);
    let old = {
        let e = e.clone();
        tokio::spawn(async move {
            e.refresh_inspections().await;
        })
    };
    backend.entered.notified().await;
    e.invalidate_inspections();
    backend
        .graph
        .lock()
        .unwrap()
        .edge(key("camera"), key("detector"), Relation::Requires);
    backend.paused.store(false, Ordering::SeqCst);
    backend.resume.notify_one();
    old.await.unwrap();
    assert!(e.reports()["robot"].stale);
    let report = e.inspect("robot", Action::Start).await.unwrap();
    assert!(!report.stale);
    assert!(report.blocked());
}
#[tokio::test]
async fn approval_changes_invalidate_cached_inspection() {
    let e = Engine::new(config(), Arc::new(Fake::default()), None, "boot".into()).unwrap();
    e.inspect("robot", Action::Start).await.unwrap();
    assert!(!e.reports()["robot"].stale);
    e.approve("sensors_ready").unwrap();
    assert!(e.reports()["robot"].stale);
}
#[tokio::test]
async fn operation_inspection_seeds_only_selected_scope() {
    let backend = Arc::new(Fake::default());
    let e = Engine::new(config(), backend.clone(), None, "boot".into()).unwrap();
    let report = e.inspect("perception", Action::Start).await.unwrap();
    assert_eq!(*backend.seeds.lock().unwrap(), vec![key("detector")]);
    assert_eq!(report.requested_units, vec![key("detector")]);
    assert_eq!(report.uninspected_units, vec![key("detector")]);
}
#[tokio::test]
async fn changed_preflight_is_refused_before_jobs() {
    use std::sync::atomic::Ordering;
    let backend = Arc::new(Fake::default());
    backend.paused.store(true, Ordering::SeqCst);
    let e = Engine::new(config(), backend.clone(), None, "boot".into()).unwrap();
    e.launch("robot".into(), true, Action::Start).unwrap();
    backend.entered.notified().await;
    e.invalidate_inspections();
    backend.resume.notify_one();
    let run = tokio::time::timeout(Duration::from_secs(2), e.wait())
        .await
        .unwrap();
    assert_eq!(run.status, "failed");
    assert!(backend.calls.lock().unwrap().is_empty());
    assert!(
        run.progress
            .iter()
            .any(|p| p.error.as_ref().is_some_and(|e| e.contains("stale=true")))
    );
}

#[test]
fn manager_generation_change_does_not_carry_old_invocation_into_new_baseline() {
    let c = config();
    let mut monitor = Monitor::default();
    let approved = BTreeSet::from(["sensors_ready".into()]);
    for (generation, invocation) in [(1, Some("old")), (2, None), (2, Some("new"))] {
        let mut next = observations("active");
        let o = next.get_mut("detector").unwrap();
        o.manager_generation = Some(generation);
        o.invocation_id = invocation.map(str::to_owned);
        monitor.update(&c, &Default::default(), &approved, next, &BTreeSet::new());
    }
    let events = monitor.events();
    assert_eq!(events.len(), 1);
    assert!(events[0].message.contains("Observation restored"));
}
