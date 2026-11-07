use crate::{
    inspection::{self, DependencyGraph, InspectionReport},
    model::*,
    monitor::{BackendEvent, ManagerHealth, Monitor},
};
use anyhow::{Result, ensure};
use async_trait::async_trait;
use futures_util::future::join_all;
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet},
    future::Future,
    path::PathBuf,
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::sync::Notify;
use tokio_util::sync::CancellationToken;

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Action {
    Start,
    Stop,
    Restart,
    Reinitialize,
}
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Observation {
    pub state: String,
    #[serde(default)]
    pub health: Option<String>,
    #[serde(default)]
    pub children: Vec<crate::children::ChildObservation>,
    #[serde(default)]
    pub children_error: Option<String>,
    #[serde(default)]
    pub manager_generation: Option<u64>,
    pub canonical_unit: Option<String>,
    pub invocation_id: Option<String>,
    pub sub_state: String,
    pub pid: Option<u32>,
    pub memory_bytes: Option<u64>,
    pub cpu_nsec: Option<u64>,
    pub error: Option<String>,
}
#[async_trait]
pub trait Backend: Send + Sync {
    async fn operate(&self, w: &Workload, a: Action) -> Result<()>;
    async fn operate_guarded(
        &self,
        w: &Workload,
        a: Action,
        loss: Option<&CancellationToken>,
    ) -> Result<()> {
        ensure!(
            !loss.is_some_and(CancellationToken::is_cancelled),
            "manager connection lost; outcome unknown; sequence will not resume"
        );
        self.operate(w, a).await
    }
    async fn observe(&self, w: &Workload) -> Result<Observation>;
    async fn operate_child(
        &self,
        _w: &Workload,
        _id: &str,
        _epoch: &str,
        _action: Action,
    ) -> Result<()> {
        anyhow::bail!("Backend does not support child operations")
    }
    async fn readiness(&self, w: &Workload) -> Result<bool> {
        let observation = self.observe(w).await?;
        Ok(observation.state == "active"
            && observation
                .health
                .as_deref()
                .is_none_or(|health| health == "healthy"))
    }
    async fn logs(&self, _w: &Workload) -> Result<String> {
        anyhow::bail!("Backend does not provide logs")
    }
    fn recently_operated(&self, w: &Workload) -> bool {
        self.recently_requested(&UnitKey::from(w))
    }

    async fn scan(&self, scope: Scope) -> Result<Vec<String>>;
    async fn dependencies(&self, _seeds: &[UnitKey]) -> Result<DependencyGraph> {
        Ok(DependencyGraph {
            incomplete: vec!["Backend does not provide dependency inspection".into()],
            ..Default::default()
        })
    }
    fn subscribe_events(&self) -> Option<tokio::sync::broadcast::Receiver<BackendEvent>> {
        None
    }
    fn inspection_revision(&self) -> u64 {
        0
    }
    fn monitoring_health(&self) -> BTreeMap<Scope, ManagerHealth> {
        BTreeMap::new()
    }
    // A captured token belongs to one connection generation and stays cancelled
    // after reconnect. It never cancels an already-issued backend operation.
    fn operation_loss(&self, _scope: Scope) -> Option<CancellationToken> {
        None
    }
    fn recently_requested(&self, _key: &UnitKey) -> bool {
        false
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Progress {
    pub step: String,
    pub action: Action,
    pub status: String,
    pub error: Option<String>,
}
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Run {
    pub target: String,
    pub status: String,
    pub progress: Vec<Progress>,
    pub waiting_gate: Option<String>,
}
#[derive(Serialize, Deserialize, Default)]
struct Approvals {
    boot: String,
    approved: BTreeSet<String>,
}
#[derive(Default)]
struct ExecutionState {
    run: Run,
    cancel: Option<CancellationToken>,
    started: Option<std::time::Instant>,
    finished: Option<std::time::Instant>,
}

enum Operation {
    Sequence {
        group: bool,
    },
    Child {
        parent: String,
        id: String,
        epoch: String,
    },
}
struct CachedInspection {
    report: InspectionReport,
    epoch: u64,
    backend_epoch: Option<u64>,
}
#[derive(Default)]
struct InspectionCache {
    epoch: u64,
    reports: BTreeMap<String, CachedInspection>,
}
pub struct Engine {
    pub config: Arc<Config>,
    pub backend: Arc<dyn Backend>,
    execution: Mutex<ExecutionState>,
    approvals: Mutex<Approvals>,
    notify: Notify,
    state_file: Option<PathBuf>,
    pub monitor: Mutex<Monitor>,
    reports: Mutex<InspectionCache>,
}
impl Engine {
    pub fn new(
        config: Config,
        backend: Arc<dyn Backend>,
        state_file: Option<PathBuf>,
        boot: String,
    ) -> Result<Arc<Self>> {
        config.validate()?;
        let mut a = if let Some(p) = &state_file {
            match std::fs::read(p) {
                Ok(v) => serde_json::from_slice::<Approvals>(&v)?,
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => Approvals::default(),
                Err(e) => return Err(e.into()),
            }
        } else {
            Approvals::default()
        };
        if a.boot != boot {
            a = Approvals {
                boot,
                approved: BTreeSet::new(),
            };
        }
        Ok(Arc::new(Self {
            config: Arc::new(config),
            backend,
            execution: Mutex::new(ExecutionState::default()),
            approvals: Mutex::new(a),
            notify: Notify::new(),
            state_file,
            monitor: Mutex::new(Monitor::default()),
            reports: Mutex::new(InspectionCache::default()),
        }))
    }
    pub fn snapshot(&self) -> Run {
        self.execution.lock().unwrap().run.clone()
    }
    pub fn approved(&self) -> BTreeSet<String> {
        self.approvals.lock().unwrap().approved.clone()
    }
    fn persist(&self, a: &Approvals) -> Result<()> {
        if let Some(p) = &self.state_file {
            if let Some(parent) = p.parent() {
                std::fs::create_dir_all(parent)?;
            }
            let tmp = p.with_extension("tmp");
            std::fs::write(&tmp, serde_json::to_vec(a)?)?;
            std::fs::rename(tmp, p)?;
        }
        Ok(())
    }
    pub fn approve(&self, id: &str) -> Result<()> {
        ensure!(
            matches!(self.config.gates.get(id), Some(Gate::Manual)),
            "not a manual gate: {id}"
        );
        let mut a = self.approvals.lock().unwrap();
        let mut next = Approvals {
            boot: a.boot.clone(),
            approved: a.approved.clone(),
        };
        next.approved.insert(id.into());
        self.persist(&next)?;
        *a = next;
        drop(a);
        self.invalidate_inspections();
        self.notify.notify_waiters();
        Ok(())
    }
    pub fn cancel(&self) -> Result<()> {
        let mut state = self.execution.lock().unwrap();
        let cancel = state
            .cancel
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("no active operation"))?
            .clone();
        state.run.status = "cancelling".into();
        cancel.cancel();
        Ok(())
    }
    pub fn launch(self: &Arc<Self>, target: String, group: bool, action: Action) -> Result<()> {
        if group {
            ensure!(
                self.config.groups.contains_key(&target),
                "unknown group {target}"
            );
        } else {
            ensure!(
                self.config.workloads.contains_key(&target),
                "unknown workload {target}"
            );
            ensure!(
                action != Action::Reinitialize,
                "reinitialize requires a group"
            );
        }
        self.launch_operation(target, action, Operation::Sequence { group })
    }
    pub fn launch_child(
        self: &Arc<Self>,
        parent: String,
        id: String,
        epoch: String,
        action: Action,
    ) -> Result<()> {
        ensure!(
            self.config.workloads.contains_key(&parent),
            "unknown parent workload"
        );
        ensure!(
            action != Action::Reinitialize,
            "Reinitialize requires a Group"
        );
        let target = format!("{parent}/{id}");
        self.launch_operation(target, action, Operation::Child { parent, id, epoch })
    }
    fn launch_operation(
        self: &Arc<Self>,
        target: String,
        action: Action,
        operation: Operation,
    ) -> Result<()> {
        let mut state = self.execution.lock().unwrap();
        ensure!(state.cancel.is_none(), "another operation is running");
        let cancel = CancellationToken::new();
        state.cancel = Some(cancel.clone());
        state.started = Some(std::time::Instant::now());
        state.finished = None;
        state.run = Run {
            target: target.clone(),
            status: "running".into(),
            ..Default::default()
        };
        let this = self.clone();
        tokio::spawn(async move {
            let result = match operation {
                Operation::Sequence { group } => {
                    this.execute(&target, group, action, &cancel).await
                }
                Operation::Child { parent, id, epoch } => {
                    if cancel.is_cancelled() {
                        Ok(())
                    } else {
                        this.track(
                            id.clone(),
                            action,
                            None,
                            this.backend.operate_child(
                                &this.config.workloads[&parent],
                                &id,
                                &epoch,
                                action,
                            ),
                        )
                        .await
                    }
                }
            };
            let mut state = this.execution.lock().unwrap();
            let r = &mut state.run;
            r.status = if result.is_err() {
                "failed"
            } else if cancel.is_cancelled() {
                "cancelled"
            } else {
                "completed"
            }
            .into();
            if let Err(e) = result {
                r.progress.push(Progress {
                    step: target,
                    action,
                    status: "failed".into(),
                    error: Some(format!("{e:#}")),
                });
            }
            r.waiting_gate = None;
            state.cancel = None;
            state.finished = Some(std::time::Instant::now());
        });
        Ok(())
    }
    pub async fn wait(&self) -> Run {
        loop {
            {
                let state = self.execution.lock().unwrap();
                if state.cancel.is_none() {
                    return state.run.clone();
                }
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    }

    fn plan(&self, id: &str, group: bool, action: Action) -> Result<Vec<(Action, Step)>> {
        if !group {
            return Ok(vec![(action, Step::Workload(id.into()))]);
        }
        let phases: &[Action] = match action {
            Action::Start => &[Action::Start],
            Action::Stop => &[Action::Stop],
            Action::Restart | Action::Reinitialize => &[Action::Stop, Action::Start],
        };
        let mut plan = Vec::new();
        for &phase in phases {
            for step in self.config.expand(id, phase == Action::Stop)? {
                plan.push((phase, step));
            }
        }
        Ok(plan)
    }

    fn reset_approvals(&self, group: &str) -> Result<()> {
        let mut approvals = self.approvals.lock().unwrap();
        let mut next = Approvals {
            boot: approvals.boot.clone(),
            approved: approvals.approved.clone(),
        };
        for step in self.config.expand(group, false)? {
            if let Step::Gate(id) = step {
                next.approved.remove(&id);
            }
        }
        self.persist(&next)?;
        *approvals = next;
        drop(approvals);
        self.invalidate_inspections();
        Ok(())
    }

    async fn execute(
        &self,
        id: &str,
        group: bool,
        action: Action,
        cancel: &CancellationToken,
    ) -> Result<()> {
        let plan = self.plan(id, group, action)?;
        let scopes: BTreeSet<_> = plan
            .iter()
            .flat_map(|(_, step)| match step {
                Step::Workload(id) => std::slice::from_ref(id),
                Step::Parallel(ids) => ids.as_slice(),
                _ => &[],
            })
            .filter(|id| self.config.workloads[*id].backend == BackendKind::Systemd)
            .map(|id| self.config.workloads[id].scope)
            .collect();
        let losses: Vec<_> = scopes
            .into_iter()
            .filter_map(|scope| {
                self.backend
                    .operation_loss(scope)
                    .map(|token| (scope, token))
            })
            .collect();
        if group && !cancel.is_cancelled() {
            let report = self.inspect(id, action).await?;
            ensure!(
                cancel.is_cancelled() || (!report.blocked() && !report.stale),
                "Dependency inspection blocked this operation (stale={}): {}",
                report.stale,
                report
                    .diagnostics
                    .iter()
                    .filter(|d| d.level == "error")
                    .map(|d| d.message.as_str())
                    .collect::<Vec<_>>()
                    .join("; ")
            );
        }
        if action == Action::Reinitialize && !cancel.is_cancelled() {
            self.reset_approvals(id)?;
        }
        let mut failure = None;
        for (action, step) in plan {
            if failure.is_none()
                && let Some((scope, _)) = losses.iter().find(|(_, token)| token.is_cancelled())
            {
                failure = Some(anyhow::anyhow!(
                    "{scope} manager connection lost during operation; outcome unknown; sequence will not resume"
                ));
            }
            if cancel.is_cancelled()
                || failure.is_some()
                || (action == Action::Stop && matches!(step, Step::Gate(_)))
            {
                self.execution.lock().unwrap().run.progress.push(Progress {
                    step: step_label(&step),
                    action,
                    status: "skipped".into(),
                    error: None,
                });
                continue;
            }
            if let Err(error) = self.run_step(&step, action, cancel, &losses).await {
                failure = Some(error);
            }
        }
        match failure {
            Some(error) => Err(error),
            None => Ok(()),
        }
    }

    async fn run_step(
        &self,
        step: &Step,
        action: Action,
        cancel: &CancellationToken,
        losses: &[(Scope, CancellationToken)],
    ) -> Result<()> {
        let gate_cancel = if matches!(step, Step::Gate(_)) {
            Some(cancel)
        } else {
            None
        };
        self.track(step_label(step), action, gate_cancel, async {
            match step {
                Step::Workload(id) => self.operate_workload(id, action, losses).await,
                Step::Parallel(ids) => {
                    let results = join_all(ids.iter().map(|id| {
                        self.track(id.clone(), action, None, self.operate_workload(id, action, losses))
                    }))
                    .await;
                    let errors: Vec<_> = results
                        .into_iter()
                        .filter_map(Result::err)
                        .map(|error| format!("{error:#}"))
                        .collect();
                    ensure!(errors.is_empty(), "{}", errors.join("; "));
                    Ok(())
                }
                Step::Gate(id) => tokio::select! {
                    biased;
                    scope = wait_for_loss(losses) => anyhow::bail!("{scope} manager connection lost during gate wait; outcome unknown; sequence will not resume"),
                    result = self.gate(id, cancel) => result,
                },
                Step::Group(_) => unreachable!("groups are expanded before execution"),
            }
        })
        .await
    }

    // Every step and Parallel member uses the same progress lifecycle.
    // Cancel skips a waiting gate; already-issued workload operations retain their result.
    async fn track(
        &self,
        step: String,
        action: Action,
        gate_cancel: Option<&CancellationToken>,
        operation: impl Future<Output = Result<()>>,
    ) -> Result<()> {
        let index = {
            let mut state = self.execution.lock().unwrap();
            let index = state.run.progress.len();
            state.run.progress.push(Progress {
                step,
                action,
                status: "running".into(),
                error: None,
            });
            index
        };
        let result = operation.await;
        let status = if result.is_err() {
            "failed"
        } else if gate_cancel.is_some_and(CancellationToken::is_cancelled) {
            "skipped"
        } else {
            "completed"
        };
        let mut state = self.execution.lock().unwrap();
        state.run.progress[index].status = status.into();
        state.run.progress[index].error = result.as_ref().err().map(|error| format!("{error:#}"));
        result
    }

    async fn operate_workload(
        &self,
        id: &str,
        action: Action,
        losses: &[(Scope, CancellationToken)],
    ) -> Result<()> {
        let workload = &self.config.workloads[id];
        let loss = losses
            .iter()
            .find(|(scope, _)| *scope == workload.scope)
            .map(|(_, token)| token);
        self.backend.operate_guarded(workload, action, loss).await
    }
    async fn gate(&self, id: &str, c: &CancellationToken) -> Result<()> {
        self.execution.lock().unwrap().run.waiting_gate = Some(id.into());
        let result = match &self.config.gates[id] {
            Gate::Manual => {
                loop {
                    let notified = self.notify.notified();
                    tokio::pin!(notified);
                    notified.as_mut().enable();
                    if self.approvals.lock().unwrap().approved.contains(id) {
                        break;
                    }
                    tokio::select! {_=c.cancelled()=>break,_=notified=>{}}
                }
                Ok(())
            }
            Gate::ContainerReady {
                workload,
                timeout_ms,
                interval_ms,
            } => {
                let check = async {
                    loop {
                        if self
                            .backend
                            .readiness(&self.config.workloads[workload])
                            .await
                            .unwrap_or(false)
                        {
                            break;
                        }
                        tokio::time::sleep(Duration::from_millis(*interval_ms)).await;
                    }
                };
                tokio::select! {_=c.cancelled()=>Ok(()),result=tokio::time::timeout(Duration::from_millis(*timeout_ms),check)=>result.map_err(|_|anyhow::anyhow!("Container readiness gate {id} timed out"))}
            }
            Gate::Http {
                url,
                timeout_ms,
                interval_ms,
                expected_status,
            } => {
                let client = reqwest::Client::builder()
                    .redirect(reqwest::redirect::Policy::none())
                    .build()?;
                let poll = async {
                    loop {
                        if let Ok(r) = client.get(url).send().await
                            && r.status().as_u16() == *expected_status
                        {
                            return;
                        }
                        tokio::time::sleep(Duration::from_millis(*interval_ms)).await;
                    }
                };
                tokio::select! {_=c.cancelled()=>Ok(()),r=tokio::time::timeout(Duration::from_millis(*timeout_ms),poll)=>r.map_err(|_|anyhow::anyhow!("HTTP gate {id} timed out"))}
            }
        };
        self.execution.lock().unwrap().run.waiting_gate = None;
        result
    }
    pub fn operation_overlaps(&self, observed: std::time::Instant) -> bool {
        let state = self.execution.lock().unwrap();
        state.started.is_some_and(|start| {
            observed >= start && state.finished.is_none_or(|end| observed <= end)
        })
    }
    pub fn reports(&self) -> BTreeMap<String, InspectionReport> {
        let cache = self.reports.lock().unwrap();
        let backend_epoch = self.backend.inspection_revision();
        cache
            .reports
            .iter()
            .map(|(id, cached)| {
                let mut report = cached.report.clone();
                report.stale |= cached.epoch != cache.epoch
                    || cached
                        .backend_epoch
                        .is_some_and(|generation| generation != backend_epoch);
                (id.clone(), report)
            })
            .collect()
    }
    fn publish_inspection(
        &self,
        id: &str,
        mut report: InspectionReport,
        epoch: u64,
        backend_epoch: Option<u64>,
    ) -> InspectionReport {
        let backend_epoch = backend_epoch.filter(|_| !report.requested_units.is_empty());
        let mut cache = self.reports.lock().unwrap();
        if cache.epoch != epoch
            || backend_epoch
                .is_some_and(|generation| self.backend.inspection_revision() != generation)
        {
            report.stale = true;
            report
                .incomplete
                .push("Inputs changed during inspection; retry after the change completes".into());
        } else {
            cache.reports.insert(
                id.into(),
                CachedInspection {
                    report: report.clone(),
                    epoch,
                    backend_epoch,
                },
            );
        }
        report
    }
    async fn dependency_snapshot(&self, seeds: &[UnitKey]) -> DependencyGraph {
        if seeds.is_empty() {
            return DependencyGraph::default();
        }
        match self.backend.dependencies(seeds).await {
            Ok(graph) => graph,
            Err(e) => DependencyGraph {
                incomplete: vec![format!("Inspection failed: {e:#}")],
                ..Default::default()
            },
        }
    }
    pub async fn inspect(&self, group: &str, action: Action) -> Result<InspectionReport> {
        let steps = self.config.expand(group, false)?;
        // Operation scope first. Reverse dependency properties reveal outside impacts.
        let mut seeds = Vec::new();
        for step in steps {
            match step {
                Step::Workload(id) => seeds.extend(self.config.workloads[&id].systemd_key()),
                Step::Parallel(ids) => {
                    for id in ids {
                        seeds.extend(self.config.workloads[&id].systemd_key());
                    }
                }
                _ => {}
            }
        }
        let epoch = self.reports.lock().unwrap().epoch;
        let backend_epoch = Some(self.backend.inspection_revision());
        let approved = self.approved();
        let graph = self.dependency_snapshot(&seeds).await;
        let report = inspection::inspect(&self.config, group, action, &approved, &graph)?;
        Ok(self.publish_inspection(group, report, epoch, backend_epoch))
    }
    pub fn invalidate_inspections(&self) {
        let mut cache = self.reports.lock().unwrap();
        cache.epoch = cache.epoch.wrapping_add(1);
        for cached in cache.reports.values_mut() {
            cached.report.stale = true;
        }
    }
    pub fn inspections_stale(&self) -> bool {
        let reports = self.reports();
        reports.len() != self.config.groups.len() || reports.values().any(|r| r.stale)
    }
    pub async fn refresh_inspections(&self) {
        if self.config.groups.is_empty() {
            return;
        }
        let epoch = self.reports.lock().unwrap().epoch;
        let backend_epoch = Some(self.backend.inspection_revision());
        let approved = self.approved();
        let seeds: Vec<_> = self
            .config
            .workloads
            .values()
            .filter_map(Workload::systemd_key)
            .collect();
        let graph = self.dependency_snapshot(&seeds).await;
        for id in self.config.groups.keys() {
            if let Ok(report) =
                inspection::inspect(&self.config, id, Action::Restart, &approved, &graph)
            {
                self.publish_inspection(id, report, epoch, backend_epoch);
            }
        }
    }
    pub async fn observations(&self) -> BTreeMap<String, Observation> {
        join_all(self.config.workloads.iter().map(|(id, w)| async move {
            (
                id.clone(),
                tokio::time::timeout(Duration::from_secs(5), self.backend.observe(w))
                    .await
                    .map_err(anyhow::Error::from)
                    .and_then(|r| r)
                    .unwrap_or_else(|e| Observation {
                        state: "unknown".into(),
                        error: Some(format!("{e:#}")),
                        ..Default::default()
                    }),
            )
        }))
        .await
        .into_iter()
        .collect()
    }
}

async fn wait_for_loss(losses: &[(Scope, CancellationToken)]) -> Scope {
    if losses.is_empty() {
        return std::future::pending().await;
    }
    futures_util::future::select_all(losses.iter().map(|(scope, token)| {
        Box::pin(async move {
            token.cancelled().await;
            *scope
        })
    }))
    .await
    .0
}

fn step_label(step: &Step) -> String {
    match step {
        Step::Workload(id) | Step::Gate(id) => id.clone(),
        Step::Parallel(ids) => format!("parallel: {}", ids.join(", ")),
        Step::Group(_) => unreachable!("groups are expanded before execution"),
    }
}
