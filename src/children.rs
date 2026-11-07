//! Runtime children are observations/control handles, never Lifecycle Group steps.
use serde::{Deserialize, Serialize};
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ChildKind {
    Process,
    Service,
    Component,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ChildAction {
    Start,
    Stop,
    Restart,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ChildObservation {
    pub id: String,
    pub epoch: String,
    pub name: String,
    pub kind: ChildKind,
    #[serde(default)]
    pub parent: Option<String>,
    pub state: String,
    pub pid: Option<u32>,
    pub pid_namespace: String,
    pub cpu_percent: Option<f64>,
    pub memory_bytes: Option<u64>,
    pub exit_code: Option<i32>,
    #[serde(default)]
    pub runtime_id: Option<String>,
    pub actions: Vec<ChildAction>,
    pub reason: Option<String>,
}
