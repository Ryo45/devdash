//! Explicit backend dispatch; no registration/plugin framework.
use crate::{
    children::*,
    docker::{self, DockerApi},
    engine::{Action, Backend, Observation},
    inspection::DependencyGraph,
    model::*,
    monitor::{BackendEvent, ManagerHealth},
    systemd::Systemd,
};
use anyhow::{Context, Result, bail, ensure};
use async_trait::async_trait;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    sync::Mutex,
    time::{Duration, Instant},
};
use tokio_util::sync::CancellationToken;
pub struct Backends {
    pub systemd: Systemd,
    requests: Mutex<BTreeMap<String, Instant>>,
}
impl Backends {
    pub async fn connect() -> Result<Self> {
        Ok(Self {
            systemd: Systemd::connect().await?,
            requests: Mutex::new(BTreeMap::new()),
        })
    }
    fn requested(&self, w: &Workload) {
        let mut requests = self.requests.lock().unwrap();
        requests.retain(|_, t| t.elapsed() < Duration::from_secs(5));
        requests.insert(w.identity(), Instant::now());
    }
}
fn child_action(action: Action) -> Result<ChildAction> {
    Ok(match action {
        Action::Start => ChildAction::Start,
        Action::Stop => ChildAction::Stop,
        Action::Restart => ChildAction::Restart,
        Action::Reinitialize => bail!("Reinitialize only applies to Groups"),
    })
}
async fn host_rpc(c: &ControllerConfig, request: Value) -> Result<Value> {
    let kind = match c.kind {
        ControllerKind::Supervisord => "supervisord",
        ControllerKind::Roslaunch => "roslaunch",
    };
    let result = tokio::time::timeout(
        Duration::from_secs(60),
        tokio::process::Command::new(&c.python)
            .args([
                "-c",
                include_str!("../bridges/controller_client.py"),
                kind,
                c.socket.to_str().context("socket path must be UTF-8")?,
                &request.to_string(),
            ])
            .kill_on_drop(true)
            .output(),
    )
    .await
    .context("Controller RPC timed out; outcome unknown; not retried")??;
    ensure!(
        result.stdout.len() <= 4 * 1024 * 1024,
        "Controller reply too large"
    );
    let reply: Value = serde_json::from_slice(&result.stdout)
        .context("Controller client requires Python 3 with stdlib")?;
    ensure!(
        result.status.success() && reply["ok"] == true,
        "Controller: {}",
        reply["error"]
            .as_str()
            .unwrap_or("RPC failed; outcome unknown")
    );
    Ok(reply["data"].clone())
}
async fn host_children(w: &Workload, c: &ControllerConfig) -> Result<Vec<ChildObservation>> {
    let reply = host_rpc(c, json!({"command":"snapshot"})).await?;
    let prefix = format!("host:{}", w.identity());
    match c.kind {
        ControllerKind::Supervisord => {
            let mut children = docker::supervisor_children(&prefix, &reply)?;
            for child in &mut children {
                child.pid_namespace = "host supervisor".into();
                child.reason=Some("supervisord owns lifecycle; resources are not inferred from a logical service name".into());
            }
            Ok(children)
        }
        ControllerKind::Roslaunch => {
            let mut children: Vec<ChildObservation> =
                serde_json::from_value(reply["children"].clone())?;
            for child in &mut children {
                child.epoch = format!("{prefix}/{}", child.epoch);
            }
            Ok(children)
        }
    }
}
#[async_trait]
impl Backend for Backends {
    async fn operate(&self, w: &Workload, action: Action) -> Result<()> {
        self.requested(w);
        match w.backend {
            BackendKind::Systemd => self.systemd.operate(w, action).await,
            BackendKind::Docker => {
                let config = w.docker.as_ref().context("missing Docker reference")?;
                let api = DockerApi::connect(config).await?;
                let target = api
                    .resolve(config)
                    .await?
                    .context("Container not found; no container was created")?;
                api.operate(&target, action).await
            }
        }
    }
    async fn operate_guarded(
        &self,
        w: &Workload,
        action: Action,
        loss: Option<&CancellationToken>,
    ) -> Result<()> {
        if w.backend == BackendKind::Systemd {
            self.requested(w);
            self.systemd.operate_guarded(w, action, loss).await
        } else {
            self.operate(w, action).await
        }
    }
    async fn observe(&self, w: &Workload) -> Result<Observation> {
        match w.backend {
            BackendKind::Systemd => {
                let mut observation = self.systemd.observe(w).await?;
                if let Some(c) = &w.controller {
                    match host_children(w, c).await {
                        Ok(children) => observation.children = children,
                        Err(e) => observation.children_error = Some(format!("{e:#}")),
                    }
                }
                Ok(observation)
            }
            BackendKind::Docker => {
                let config = w.docker.as_ref().context("missing Docker reference")?;
                let api = DockerApi::connect(config).await?;
                match api.resolve(config).await? {
                    Some(target) => Ok(api.observe(w, &target).await),
                    None => Ok(Observation {
                        state: "not_found".into(),
                        ..Default::default()
                    }),
                }
            }
        }
    }
    async fn operate_child(
        &self,
        w: &Workload,
        id: &str,
        epoch: &str,
        action: Action,
    ) -> Result<()> {
        self.requested(w);
        let action = child_action(action)?;
        let controller = w
            .controller
            .as_ref()
            .context("No controller configured: OS processes are observation-only")?;
        if w.backend == BackendKind::Docker {
            let config = w.docker.as_ref().context("missing Docker reference")?;
            let api = DockerApi::connect(config).await?;
            let target = api.resolve(config).await?.context("Container not found")?;
            api.operate_child(&target, controller, id, epoch, action)
                .await
        } else {
            let children = host_children(w, controller).await?;
            let child = children
                .iter()
                .find(|c| c.id == id)
                .context("Child no longer exists; refresh")?;
            ensure!(
                child.epoch == epoch,
                "Controller changed; refresh before operating"
            );
            ensure!(
                child.actions.contains(&action),
                "Action not supported: {}",
                child
                    .reason
                    .as_deref()
                    .unwrap_or("current controller state")
            );
            let prefix = format!("host:{}/", w.identity());
            let inner = epoch
                .strip_prefix(&prefix)
                .context("invalid controller epoch")?;
            host_rpc(
                controller,
                json!({"command":"operate","id":id,"epoch":inner,"action":action}),
            )
            .await?;
            Ok(())
        }
    }
    async fn readiness(&self, w: &Workload) -> Result<bool> {
        if w.backend == BackendKind::Docker {
            let config = w.docker.as_ref().context("missing Docker reference")?;
            let api = DockerApi::connect(config).await?;
            Ok(api
                .resolve(config)
                .await?
                .is_some_and(|target| docker::is_ready(&target.info)))
        } else {
            let observation = self.systemd.observe(w).await?;
            Ok(observation.state == "active")
        }
    }
    async fn logs(&self, w: &Workload) -> Result<String> {
        match w.backend {
            BackendKind::Systemd => crate::systemd::logs(w).await,
            BackendKind::Docker => {
                let config = w.docker.as_ref().context("missing Docker reference")?;
                let api = DockerApi::connect(config).await?;
                let target = api.resolve(config).await?.context("Container not found")?;
                api.logs(&target).await
            }
        }
    }
    async fn scan(&self, scope: Scope) -> Result<Vec<String>> {
        self.systemd.scan(scope).await
    }
    async fn dependencies(&self, seeds: &[UnitKey]) -> Result<DependencyGraph> {
        self.systemd.dependencies(seeds).await
    }
    fn subscribe_events(&self) -> Option<tokio::sync::broadcast::Receiver<BackendEvent>> {
        self.systemd.subscribe_events()
    }
    fn inspection_revision(&self) -> u64 {
        self.systemd.inspection_revision()
    }
    fn monitoring_health(&self) -> BTreeMap<Scope, ManagerHealth> {
        self.systemd.monitoring_health()
    }
    fn operation_loss(&self, scope: Scope) -> Option<CancellationToken> {
        self.systemd.operation_loss(scope)
    }
    fn recently_requested(&self, key: &UnitKey) -> bool {
        self.systemd.recently_requested(key)
    }
    fn recently_operated(&self, w: &Workload) -> bool {
        if w.backend == BackendKind::Systemd && self.systemd.recently_requested(&UnitKey::from(w)) {
            true
        } else {
            self.requests
                .lock()
                .unwrap()
                .get(&w.identity())
                .is_some_and(|t| t.elapsed() < Duration::from_secs(5))
        }
    }
}
