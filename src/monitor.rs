//! Bounded, in-memory observations; never performs lifecycle operations.
use crate::{
    engine::{Observation, Run},
    model::*,
};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EventKind {
    Job,
    UnitNew,
    UnitRemoved,
    Reloading,
    Reloaded,
    Disconnected,
    Reconnected,
    Gap,
    StateChanged,
    ChildChanged,
    ObservationLost,
    ObservationRestored,
    GateMismatch,
    Notification,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Correlation {
    Requested,
    RecentRequest,
    Unrelated,
    Unknown,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ManagerHealth {
    pub available: bool,
    pub reason: Option<String>,
}
#[derive(Clone, Debug)]
pub struct BackendEvent {
    pub scope: Scope,
    pub unit: Option<UnitKey>,
    pub kind: EventKind,
    pub message: String,
    pub correlation: Correlation,
    pub observed_at: std::time::Instant,
    pub observed_unix_ms: u64,
    pub job_path: Option<String>,
    pub job_result: Option<String>,
}
impl BackendEvent {
    pub fn new(scope: Scope, unit: Option<UnitKey>, kind: EventKind, message: String) -> Self {
        Self {
            scope,
            unit,
            kind,
            message,
            correlation: Correlation::Unknown,
            observed_at: std::time::Instant::now(),
            observed_unix_ms: unix_ms(),
            job_path: None,
            job_result: None,
        }
    }
}
pub fn unix_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Event {
    pub id: u64,
    pub level: String,
    pub workload: Option<String>,
    pub message: String,
    pub scope: Option<Scope>,
    pub kind: EventKind,
    pub observed_unix_ms: u64,
    pub job_path: Option<String>,
    pub job_result: Option<String>,
    pub correlation: Correlation,
}
#[derive(Default)]
pub struct Monitor {
    previous: Option<BTreeMap<String, Observation>>,
    outages: BTreeSet<String>,
    events: VecDeque<Event>,
    sequence: u64,
    gate_issues: BTreeSet<(String, String)>,
    unit_metadata_events: BTreeMap<UnitKey, std::time::Instant>,
    pub stream_gap: Option<String>,
}
impl Monitor {
    pub fn events(&self) -> Vec<Event> {
        self.events.iter().cloned().collect()
    }
    fn push(&mut self, mut event: Event) {
        self.sequence += 1;
        event.id = self.sequence;
        self.events.push_back(event);
        while self.events.len() > 200 {
            self.events.pop_front();
        }
    }
    pub fn record(&mut self, level: &str, workload: Option<String>, message: String) {
        self.record_kind(
            level,
            workload,
            EventKind::Notification,
            Correlation::Unknown,
            message,
        );
    }
    fn record_kind(
        &mut self,
        level: &str,
        workload: Option<String>,
        kind: EventKind,
        correlation: Correlation,
        message: String,
    ) {
        self.push(Event {
            id: 0,
            level: level.into(),
            workload,
            message,
            scope: None,
            kind,
            observed_unix_ms: unix_ms(),
            job_path: None,
            job_result: None,
            correlation,
        });
    }
    pub fn update(
        &mut self,
        c: &Config,
        run: &Run,
        approved: &BTreeSet<String>,
        current: BTreeMap<String, Observation>,
        related: &BTreeSet<String>,
    ) {
        let baseline = self.previous.is_none();
        let mut known = self.previous.take().unwrap_or_default();
        for (id, now) in &current {
            if now.state == "unknown" {
                if self.outages.insert(id.clone()) && !baseline {
                    self.record_kind("warning",Some(id.clone()),EventKind::ObservationLost,Correlation::Unknown,"Observation unavailable; last known state retained; history may have a gap".into());
                }
                continue;
            }
            let restored = self.outages.remove(id);
            let new_generation = known.get(id).is_some_and(|old| {
                old.manager_generation.is_some()
                    && now.manager_generation.is_some()
                    && old.manager_generation != now.manager_generation
            });
            if (restored || new_generation) && !baseline {
                self.record_kind("info",Some(id.clone()),EventKind::ObservationRestored,Correlation::Unknown,format!("Observation restored; current state {}; transitions during the gap are unknown",now.state));
            } else if let Some(old) = known.get(id) {
                let invocation_changed =
                    matches!((&old.invocation_id,&now.invocation_id),(Some(a),Some(b)) if a!=b);
                if old.state != now.state || old.sub_state != now.sub_state || invocation_changed {
                    let recent = related.contains(id);
                    let level = if now.state == "not_found"
                        || (!recent && matches!(run.status.as_str(), "running" | "cancelling"))
                    {
                        "warning"
                    } else {
                        "info"
                    };
                    let attribution = if recent {
                        "recent devdash request; causal relationship unconfirmed"
                    } else {
                        "not correlated with a devdash request; origin unknown"
                    };
                    self.record_kind(
                        level,
                        Some(id.clone()),
                        EventKind::StateChanged,
                        if recent {
                            Correlation::RecentRequest
                        } else {
                            Correlation::Unrelated
                        },
                        format!(
                            "{} → {} ({attribution}){}",
                            old.state,
                            now.state,
                            if invocation_changed {
                                "; invocation changed"
                            } else {
                                ""
                            }
                        ),
                    );
                }
            }
            if !baseline
                && !restored
                && !new_generation
                && let Some(old) = known.get(id)
            {
                if old.health != now.health {
                    self.record_kind(
                        "info",
                        Some(id.clone()),
                        EventKind::Notification,
                        Correlation::Unknown,
                        format!(
                            "Container health {:?} → {:?}; no recovery action requested",
                            old.health, now.health
                        ),
                    );
                }
                if old.children_error != now.children_error
                    && let Some(error) = &now.children_error
                {
                    self.record_kind(
                        "warning",
                        Some(id.clone()),
                        EventKind::Notification,
                        Correlation::Unknown,
                        format!("Child observation unavailable: {error}"),
                    );
                }
                for child in now
                    .children
                    .iter()
                    .filter(|c| c.kind != crate::children::ChildKind::Process)
                {
                    if let Some(previous) = old
                        .children
                        .iter()
                        .find(|c| c.id == child.id && c.epoch == child.epoch)
                        && (previous.state != child.state || previous.pid != child.pid)
                    {
                        self.record_kind(
                            "info",
                            Some(id.clone()),
                            EventKind::ChildChanged,
                            if related.contains(id) {
                                Correlation::RecentRequest
                            } else {
                                Correlation::Unrelated
                            },
                            format!(
                                "Child {}: {} → {}; controller observation, origin not proven",
                                child.id, previous.state, child.state
                            ),
                        );
                    }
                }
            }
            let mut retained = now.clone();
            if !new_generation && let Some(old) = known.get(id) {
                if retained.invocation_id.is_none() {
                    retained.invocation_id = old.invocation_id.clone();
                }
                if retained.canonical_unit.is_none() {
                    retained.canonical_unit = old.canonical_unit.clone();
                }
            }
            known.insert(id.clone(), retained);
        }
        let mut issues: BTreeSet<_> = c
            .manual_followers()
            .into_iter()
            .filter(|(gate, id)| {
                !approved.contains(gate)
                    && current.get(id).is_some_and(|o| {
                        matches!(o.state.as_str(), "active" | "activating" | "reloading")
                    })
            })
            .collect();
        // An outage is not evidence that a readiness mismatch was resolved.
        issues.extend(
            self.gate_issues
                .iter()
                .filter(|(gate, id)| {
                    !approved.contains(gate)
                        && current.get(id).is_some_and(|o| o.state == "unknown")
                })
                .cloned(),
        );
        for (gate, id) in issues
            .difference(&self.gate_issues)
            .cloned()
            .collect::<Vec<_>>()
        {
            self.record_kind("warning",Some(id.clone()),EventKind::GateMismatch,Correlation::Unknown,format!("Manual gate {gate} is unapproved, but {id} is running. Observed mismatch; origin unknown"));
        }
        self.gate_issues = issues;
        self.previous = Some(known);
    }
}

/// Events keep draining while state queries and dependency inspections are in flight.
pub async fn watch(
    engine: std::sync::Arc<crate::engine::Engine>,
    observations: std::sync::Arc<tokio::sync::RwLock<BTreeMap<String, Observation>>>,
    mut events: Option<tokio::sync::broadcast::Receiver<BackendEvent>>,
    baseline_started: std::time::Instant,
) {
    use tokio::sync::broadcast::error::RecvError;
    use tokio_util::task::AbortOnDropHandle;
    let mut interval = tokio::time::interval(std::time::Duration::from_secs(1));
    let mut inspection = spawn_inspection(engine.clone());
    let mut poll: Option<AbortOnDropHandle<BTreeMap<String, Observation>>> = None;
    loop {
        tokio::select! {
            _=interval.tick()=>{
                if poll.is_none(){let e=engine.clone();poll=Some(AbortOnDropHandle::new(tokio::spawn(async move{e.observations().await})));}
                if engine.inspections_stale()&&inspection.is_finished(){inspection=spawn_inspection(engine.clone());}
            },
            result=async{match poll.as_mut(){Some(task)=>task.await,None=>std::future::pending().await}}=>{
                poll=None;
                if let Ok(next)=result{
                    let changed=observations.read().await.iter().any(|(id,old)|next.get(id).is_some_and(|now|(old.state=="not_found")!=(now.state=="not_found")));
                    if changed{engine.invalidate_inspections();}
                    let related=engine.config.workloads.iter().filter(|(_,w)|engine.backend.recently_operated(w)).map(|(id,_)|id.clone()).collect();
                    engine.monitor.lock().unwrap().update(&engine.config,&engine.snapshot(),&engine.approved(),next.clone(),&related);
                    *observations.write().await=next;
                }else{engine.monitor.lock().unwrap().stream_gap=Some("State query task failed; observation continuity unknown".into());}
            },
            event=async{match events.as_mut(){Some(receiver)=>receiver.recv().await,None=>std::future::pending().await}}=>{
                match event{
                    Ok(event)=>{
                        if event.observed_at<baseline_started{continue;}
                        if !engine.config.workloads.values().any(|w|w.backend==BackendKind::Systemd && w.scope==event.scope){continue;}
                        // Systemd updates its revision at receipt, before this queued
                        // notification. Avoid invalidating unrelated Docker-only reports.
                        if event.kind==EventKind::Gap{engine.invalidate_inspections();}
                        if matches!(event.kind,EventKind::Disconnected|EventKind::Reconnected){
                            engine.monitor.lock().unwrap().stream_gap=Some(format!("{} manager event history has a gap; current state is the new baseline",event.scope));
                        }
                        let current=observations.read().await;
                        record_backend_event(&engine,&current,event);
                    },
                    Err(RecvError::Lagged(count))=>{
                        engine.invalidate_inspections();
                        let mut monitor=engine.monitor.lock().unwrap();
                        let reason=format!("Missed {count} systemd events; event history incomplete");
                        monitor.stream_gap=Some(reason.clone());monitor.record_kind("warning",None,EventKind::Gap,Correlation::Unknown,reason);
                    },
                    Err(RecvError::Closed)=>{
                        let mut monitor=engine.monitor.lock().unwrap();monitor.stream_gap=Some("Event channel closed; monitoring unavailable".into());
                        monitor.record("warning",None,"Event channel closed; polling only".into());events=None;
                    },
                }
            }
        }
    }
}
fn spawn_inspection(
    engine: std::sync::Arc<crate::engine::Engine>,
) -> tokio_util::task::AbortOnDropHandle<()> {
    tokio_util::task::AbortOnDropHandle::new(tokio::spawn(async move {
        engine.refresh_inspections().await;
    }))
}
fn record_backend_event(
    engine: &crate::engine::Engine,
    current: &BTreeMap<String, Observation>,
    event: BackendEvent,
) {
    let overlapping = engine.operation_overlaps(event.observed_at);
    let mut monitor = engine.monitor.lock().unwrap();
    let ids: Vec<_> = engine
        .config
        .workloads
        .iter()
        .filter(|(id, w)| {
            w.backend == BackendKind::Systemd
                && w.scope == event.scope
                && event.unit.as_ref().is_some_and(|key| {
                    w.unit == key.unit
                        || current
                            .get(*id)
                            .and_then(|o| o.canonical_unit.as_deref())
                            .or_else(|| {
                                monitor
                                    .previous
                                    .as_ref()
                                    .and_then(|p| p.get(*id))
                                    .and_then(|o| o.canonical_unit.as_deref())
                            })
                            == Some(key.unit.as_str())
                })
        })
        .map(|(id, _)| Some(id.clone()))
        .collect();
    if ids.is_empty() && event.unit.is_some() {
        return;
    }
    // LoadUnit inspection and systemd garbage collection can repeatedly load
    // and unload inactive metadata. These are not service/file transitions.
    // Bound this informational noise; never suppress jobs, state or warnings.
    let metadata = matches!(event.kind, EventKind::UnitNew | EventKind::UnitRemoved);
    if metadata && let Some(key) = &event.unit {
        if monitor
            .unit_metadata_events
            .get(key)
            .is_some_and(|previous| {
                event.observed_at.saturating_duration_since(*previous)
                    < std::time::Duration::from_secs(5)
            })
        {
            return;
        }
        monitor
            .unit_metadata_events
            .insert(key.clone(), event.observed_at);
    }
    let level = if matches!(event.kind, EventKind::Disconnected | EventKind::Gap)
        || (overlapping
            && (matches!(event.kind, EventKind::Reloading | EventKind::Reloaded)
                || (event.kind == EventKind::Job && event.correlation == Correlation::Unrelated)))
    {
        "warning"
    } else {
        "info"
    };
    let correlation = match event.correlation {
        Correlation::Requested => "devdash job",
        Correlation::RecentRequest => "recent devdash request; unconfirmed",
        Correlation::Unrelated => "not correlated with a devdash request; origin unknown",
        Correlation::Unknown => "origin unknown",
    };
    let targets = if ids.is_empty() { vec![None] } else { ids };
    for workload in targets {
        monitor.push(Event {
            id: 0,
            level: level.into(),
            workload,
            message: format!("{:?}: {} ({correlation}){}", event.kind, event.message,
                if metadata { "; unit metadata signal, limited to once per 5s; not a service or file transition" } else { "" }),
            scope: Some(event.scope),
            kind: event.kind,
            observed_unix_ms: event.observed_unix_ms,
            job_path: event.job_path.clone(),
            job_result: event.job_result.clone(),
            correlation: event.correlation,
        });
    }
}
