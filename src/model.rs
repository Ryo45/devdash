use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashSet};

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub workloads: BTreeMap<String, Workload>,
    pub groups: BTreeMap<String, Group>,
    #[serde(default)]
    pub gates: BTreeMap<String, Gate>,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Workload {
    #[serde(default)]
    pub name: String,
    pub backend: BackendKind,
    #[serde(default)]
    pub scope: Scope,
    #[serde(default)]
    pub unit: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub docker: Option<DockerConfig>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub controller: Option<ControllerConfig>,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BackendKind {
    Systemd,
    Docker,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DockerConfig {
    #[serde(default = "default_docker_socket")]
    pub socket: std::path::PathBuf,
    #[serde(default)]
    pub container: Option<String>,
    #[serde(default)]
    pub compose: Option<ComposeService>,
}
fn default_docker_socket() -> std::path::PathBuf {
    "/var/run/docker.sock".into()
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ComposeService {
    pub project: String,
    pub service: String,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ControllerConfig {
    pub kind: ControllerKind,
    pub socket: std::path::PathBuf,
    #[serde(default = "default_python")]
    pub python: String,
}
fn default_python() -> String {
    "python3".into()
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ControllerKind {
    Supervisord,
    Roslaunch,
}
impl Workload {
    pub fn systemd_key(&self) -> Option<UnitKey> {
        (self.backend == BackendKind::Systemd).then(|| UnitKey::from(self))
    }
    pub fn display_reference(&self) -> String {
        match &self.docker {
            Some(config) if self.backend == BackendKind::Docker => {
                if let Some(name) = &config.container {
                    format!("docker:{name}")
                } else if let Some(c) = &config.compose {
                    format!("compose:{}/{}", c.project, c.service)
                } else {
                    "docker:unknown".into()
                }
            }
            _ => format!("{}:{}", self.scope, self.unit),
        }
    }
    pub fn identity(&self) -> String {
        match &self.docker {
            Some(config) if self.backend == BackendKind::Docker => {
                format!("docker:{}", serde_json::to_string(config).unwrap())
            }
            _ => format!("{}:{}", self.scope, self.unit),
        }
    }
}
#[derive(
    Clone, Copy, Debug, Default, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize,
)]
#[serde(rename_all = "snake_case")]
pub enum Scope {
    #[default]
    User,
    System,
}
impl std::fmt::Display for Scope {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::User => "user",
            Self::System => "system",
        })
    }
}
impl std::str::FromStr for Scope {
    type Err = String;
    fn from_str(s: &str) -> Result<Self, String> {
        match s {
            "user" => Ok(Self::User),
            "system" => Ok(Self::System),
            _ => Err("scope must be user or system".into()),
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct UnitKey {
    pub scope: Scope,
    pub unit: String,
}
impl UnitKey {
    pub fn new(scope: Scope, unit: impl Into<String>) -> Self {
        Self {
            scope,
            unit: unit.into(),
        }
    }
}
impl From<&Workload> for UnitKey {
    fn from(w: &Workload) -> Self {
        Self::new(w.scope, w.unit.clone())
    }
}
impl std::fmt::Display for UnitKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}:{}", self.scope, self.unit)
    }
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Group {
    #[serde(default)]
    pub name: String,
    pub steps: Vec<Step>,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(from = "StepWire", into = "StepWire")]
pub enum Step {
    Workload(String),
    Group(String),
    Parallel(Vec<String>),
    Gate(String),
}
#[derive(Serialize, Deserialize)]
#[serde(untagged, deny_unknown_fields)]
enum StepWire {
    Workload { workload: String },
    Group { group: String },
    Parallel { parallel: Vec<String> },
    Gate { gate: String },
}
impl From<StepWire> for Step {
    fn from(w: StepWire) -> Self {
        match w {
            StepWire::Workload { workload } => Self::Workload(workload),
            StepWire::Group { group } => Self::Group(group),
            StepWire::Parallel { parallel } => Self::Parallel(parallel),
            StepWire::Gate { gate } => Self::Gate(gate),
        }
    }
}
impl From<Step> for StepWire {
    fn from(s: Step) -> Self {
        match s {
            Step::Workload(workload) => Self::Workload { workload },
            Step::Group(group) => Self::Group { group },
            Step::Parallel(parallel) => Self::Parallel { parallel },
            Step::Gate(gate) => Self::Gate { gate },
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum Gate {
    Manual,
    ContainerReady {
        workload: String,
        timeout_ms: u64,
        interval_ms: u64,
    },
    Http {
        url: String,
        timeout_ms: u64,
        interval_ms: u64,
        #[serde(default = "default_status")]
        expected_status: u16,
    },
}
fn default_status() -> u16 {
    200
}
impl Config {
    pub fn load(path: &std::path::Path) -> Result<Self> {
        Self::parse(
            &std::fs::read_to_string(path).with_context(|| format!("read {}", path.display()))?,
        )
        .with_context(|| format!("configuration {}", path.display()))
    }
    pub fn parse(yaml: &str) -> Result<Self> {
        let c: Self = serde_yaml::from_str(yaml)?;
        c.validate()?;
        Ok(c)
    }
    pub fn validate(&self) -> Result<()> {
        let mut units = HashSet::new();
        for (id, w) in &self.workloads {
            ensure!(!id.is_empty(), "empty workload ID");
            match w.backend {
                BackendKind::Systemd => ensure!(
                    !w.unit.is_empty()
                        && w.unit.ends_with(".service")
                        && !w.unit.contains('/')
                        && !w.unit.chars().any(char::is_whitespace)
                        && w.docker.is_none(),
                    "invalid systemd service: {id}"
                ),
                BackendKind::Docker => {
                    let d = w
                        .docker
                        .as_ref()
                        .ok_or_else(|| anyhow::anyhow!("Docker reference missing: {id}"))?;
                    ensure!(
                        w.unit.is_empty() && w.scope == Scope::User,
                        "Docker uses docker reference, not systemd unit/scope: {id}"
                    );
                    ensure!(
                        d.socket.is_absolute() && (d.container.is_some() != d.compose.is_some()),
                        "Docker needs one container name or Compose project/service: {id}"
                    );
                    if let Some(name) = &d.container {
                        ensure!(
                            !name.is_empty()
                                && !name.contains('/')
                                && !name.chars().any(char::is_whitespace),
                            "invalid container name: {id}"
                        );
                    }
                    if let Some(c) = &d.compose {
                        ensure!(
                            !c.project.is_empty() && !c.service.is_empty(),
                            "empty Compose selector: {id}"
                        );
                    }
                }
            }
            if let Some(controller) = &w.controller {
                ensure!(
                    controller.socket.is_absolute() && !controller.python.is_empty(),
                    "invalid controller reference: {id}"
                );
            }
            ensure!(
                units.insert(w.identity()),
                "duplicate backend reference: {id}"
            );
        }
        let mut workloads = HashSet::new();
        let mut groups = HashSet::new();
        let mut gates = HashSet::new();
        for (id, g) in &self.groups {
            ensure!(!id.is_empty(), "empty group ID");
            for s in &g.steps {
                match s {
                    Step::Workload(w) => {
                        ensure!(self.workloads.contains_key(w), "unknown workload {w}");
                        ensure!(workloads.insert(w), "duplicate workload {w}");
                    }
                    Step::Parallel(ws) => {
                        ensure!(!ws.is_empty(), "empty parallel in {id}");
                        for w in ws {
                            ensure!(self.workloads.contains_key(w), "unknown workload {w}");
                            ensure!(workloads.insert(w), "duplicate workload {w}");
                        }
                    }
                    Step::Group(g) => {
                        ensure!(self.groups.contains_key(g), "unknown group {g}");
                        ensure!(groups.insert(g), "duplicate group {g}");
                    }
                    Step::Gate(g) => {
                        ensure!(self.gates.contains_key(g), "unknown gate {g}");
                        ensure!(gates.insert(g), "duplicate gate {g}");
                    }
                }
            }
        }
        fn visit(c: &Config, id: &str, path: &mut HashSet<String>) -> Result<()> {
            ensure!(path.insert(id.to_string()), "group cycle at {id}");
            for s in &c.groups[id].steps {
                if let Step::Group(g) = s {
                    visit(c, g, path)?;
                }
            }
            path.remove(id);
            Ok(())
        }
        for id in self.groups.keys() {
            visit(self, id, &mut HashSet::new())?;
        }
        for (id, g) in &self.gates {
            if let Gate::ContainerReady {
                workload,
                timeout_ms,
                interval_ms,
            } = g
            {
                ensure!(
                    self.workloads
                        .get(workload)
                        .is_some_and(|w| w.backend == BackendKind::Docker)
                        && *timeout_ms > 0
                        && *interval_ms > 0,
                    "invalid container readiness gate: {id}"
                );
            }
            if let Gate::Http {
                url,
                timeout_ms,
                interval_ms,
                expected_status,
            } = g
            {
                let u = reqwest::Url::parse(url)?;
                ensure!(
                    matches!(u.scheme(), "http" | "https")
                        && *timeout_ms > 0
                        && *interval_ms > 0
                        && (100..600).contains(expected_status),
                    "invalid HTTP gate {id}"
                );
            }
        }
        Ok(())
    }
    pub fn expand(&self, id: &str, stop: bool) -> Result<Vec<Step>> {
        let g = self
            .groups
            .get(id)
            .ok_or_else(|| anyhow::anyhow!("unknown group {id}"))?;
        let mut out = Vec::new();
        let steps: Vec<_> = if stop {
            g.steps.iter().rev().collect()
        } else {
            g.steps.iter().collect()
        };
        for s in steps {
            match s {
                Step::Group(g) => out.extend(self.expand(g, stop)?),
                _ => out.push(s.clone()),
            }
        }
        Ok(out)
    }
    pub fn import(&mut self, unit: &str, scope: Scope) -> Result<String> {
        if self
            .workloads
            .values()
            .any(|w| w.backend == BackendKind::Systemd && w.unit == unit && w.scope == scope)
        {
            bail!("already imported {unit}");
        }
        let stem = unit.trim_end_matches(".service");
        let id = if self.workloads.contains_key(stem) {
            format!("{scope}:{stem}")
        } else {
            stem.into()
        };
        ensure!(!self.workloads.contains_key(&id), "ID already exists: {id}");
        self.workloads.insert(
            id.clone(),
            Workload {
                name: id.clone(),
                backend: BackendKind::Systemd,
                scope,
                unit: unit.into(),
                docker: None,
                controller: None,
            },
        );
        self.validate()?;
        Ok(id)
    }
}

impl Config {
    pub fn manual_followers(&self) -> Vec<(String, String)> {
        let mut followers = std::collections::BTreeSet::new();
        for group in self.groups.keys() {
            let mut gates = Vec::new();
            if let Ok(steps) = self.expand(group, false) {
                for step in steps {
                    match step {
                        Step::Gate(id) if matches!(self.gates[&id], Gate::Manual) => gates.push(id),
                        Step::Workload(id) => {
                            for gate in &gates {
                                followers.insert((gate.clone(), id.clone()));
                            }
                        }
                        Step::Parallel(ids) => {
                            for id in ids {
                                for gate in &gates {
                                    followers.insert((gate.clone(), id.clone()));
                                }
                            }
                        }
                        _ => {}
                    }
                }
            }
        }
        followers.into_iter().collect()
    }
}
