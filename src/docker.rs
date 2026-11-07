//! Local Docker Engine adapter. No container creation, shell supervision or reconciliation.
use crate::{
    children::*,
    engine::{Action, Observation},
    model::*,
};
use anyhow::{Context, Result, bail, ensure};
use reqwest::{Client, Method, StatusCode, Url};
use serde_json::{Value, json};
use std::time::Duration;
const LIMIT: usize = 4 * 1024 * 1024;
const CLIENT: &str = include_str!("../bridges/controller_client.py");

pub struct DockerApi {
    client: Client,
    version: String,
}
pub struct Resolved {
    pub id: String,
    pub info: Value,
}
impl Resolved {
    pub fn epoch(&self) -> Result<String> {
        Ok(format!(
            "{}@{}",
            self.id,
            self.info["State"]["StartedAt"]
                .as_str()
                .context("Docker omitted container start identity")?
        ))
    }
}
pub fn is_ready(info: &Value) -> bool {
    info["State"]["Running"] == true
        && info["State"]["Paused"] != true
        && info["State"]["Restarting"] != true
        && info["State"]["Health"]["Status"]
            .as_str()
            .is_none_or(|health| health == "healthy")
}
impl DockerApi {
    pub async fn connect(config: &DockerConfig) -> Result<Self> {
        let client = Client::builder()
            .unix_socket(config.socket.as_path())
            .timeout(Duration::from_secs(60))
            .no_proxy()
            .build()?;
        let response = client
            .get("http://localhost/version")
            .send()
            .await
            .with_context(|| {
                format!(
                    "Docker socket {} unavailable; check access permissions",
                    config.socket.display()
                )
            })?;
        let info: Value = serde_json::from_slice(&body(response).await?)?;
        let version = negotiate_version(
            info["MinAPIVersion"].as_str().unwrap_or("1.24"),
            info["ApiVersion"]
                .as_str()
                .context("Docker omitted ApiVersion")?,
        )?;
        Ok(Self { client, version })
    }
    fn url(&self, segments: &[&str]) -> Url {
        let mut url = Url::parse("http://localhost").unwrap();
        {
            let mut path = url.path_segments_mut().unwrap();
            path.push(&self.version);
            for segment in segments {
                path.push(segment);
            }
        }
        url
    }
    async fn json(
        &self,
        method: Method,
        segments: &[&str],
        payload: Option<Value>,
    ) -> Result<Value> {
        let mut request = self.client.request(method, self.url(segments));
        if let Some(payload) = payload {
            request = request
                .header("content-type", "application/json")
                .body(payload.to_string());
        }
        let bytes = body(request.send().await?).await?;
        if bytes.is_empty() {
            Ok(Value::Null)
        } else {
            Ok(serde_json::from_slice(&bytes)?)
        }
    }
    pub async fn resolve(&self, config: &DockerConfig) -> Result<Option<Resolved>> {
        let selector = if let Some(name) = &config.container {
            name.clone()
        } else {
            let compose = config
                .compose
                .as_ref()
                .context("missing container selector")?;
            let filters = json!({"label": [format!("com.docker.compose.project={}",compose.project), format!("com.docker.compose.service={}",compose.service)]}).to_string();
            let response = self
                .client
                .get(self.url(&["containers", "json"]))
                .query(&[("all", "true"), ("filters", filters.as_str())])
                .send()
                .await?;
            let rows: Vec<Value> = serde_json::from_slice(&body(response).await?)?;
            let rows: Vec<_> = rows
                .into_iter()
                .filter(|r| {
                    !r["Labels"]["com.docker.compose.oneoff"]
                        .as_str()
                        .unwrap_or("")
                        .eq_ignore_ascii_case("true")
                })
                .collect();
            ensure!(
                rows.len() <= 1,
                "Compose selector resolves to {} containers; scaled services require an explicit container name",
                rows.len()
            );
            let Some(row) = rows.first() else {
                return Ok(None);
            };
            row["Id"]
                .as_str()
                .context("Docker omitted container ID")?
                .to_owned()
        };
        let response = self
            .client
            .get(self.url(&["containers", &selector, "json"]))
            .send()
            .await?;
        if response.status() == StatusCode::NOT_FOUND {
            return Ok(None);
        }
        let info: Value = serde_json::from_slice(&body(response).await?)?;
        let id = info["Id"]
            .as_str()
            .context("Docker omitted container ID")?
            .to_owned();
        Ok(Some(Resolved { id, info }))
    }
    pub async fn operate(&self, target: &Resolved, action: Action) -> Result<()> {
        let method = match action {
            Action::Start => "start",
            Action::Stop => "stop",
            Action::Restart => "restart",
            Action::Reinitialize => bail!("reinitialize requires a Group"),
        };
        self.json(Method::POST, &["containers", &target.id, method], None)
            .await
            .with_context(|| {
                format!(
                    "Docker {method} {}; result may be unknown; request was not retried",
                    target.id
                )
            })?;
        Ok(())
    }
    pub async fn observe(&self, workload: &Workload, target: &Resolved) -> Observation {
        let state = &target.info["State"];
        let running = state["Running"].as_bool().unwrap_or(false);
        let mut observation = Observation {
            state: match state["Status"].as_str() {
                Some("running") => "active",
                Some("restarting") => "activating",
                Some("paused") => "paused",
                Some("dead") => "failed",
                Some("exited") if state["ExitCode"].as_i64().is_some_and(|code| code != 0) => {
                    "failed"
                }
                _ => "inactive",
            }
            .into(),
            sub_state: state["Status"].as_str().unwrap_or("unknown").into(),
            health: state["Health"]["Status"].as_str().map(str::to_owned),
            canonical_unit: Some(target.id.clone()),
            invocation_id: state["StartedAt"].as_str().map(str::to_owned),
            pid: state["Pid"]
                .as_u64()
                .filter(|p| *p > 0)
                .and_then(|p| u32::try_from(p).ok()),
            ..Default::default()
        };
        // Container-wide resource figures and health remain separate from children.
        if running {
            if let Ok(response) = self
                .client
                .get(self.url(&["containers", &target.id, "stats"]))
                .query(&[("stream", "false"), ("one-shot", "true")])
                .send()
                .await
                && let Ok(bytes) = body(response).await
                && let Ok(stats) = serde_json::from_slice::<Value>(&bytes)
            {
                observation.cpu_nsec = stats["cpu_stats"]["cpu_usage"]["total_usage"].as_u64();
                observation.memory_bytes = stats["memory_stats"]["usage"].as_u64();
            }
            match self.processes(target).await {
                Ok(children) => observation.children = children,
                Err(error) => {
                    observation.children_error = Some(format!("Process inspection: {error:#}"))
                }
            }
            if let Some(controller) = &workload.controller {
                match self.controller_children(target, controller).await {
                    Ok(children) => observation.children.extend(children),
                    Err(error) => {
                        observation.children_error = Some(format!(
                            "Controller unavailable; OS processes are observation-only: {error:#}"
                        ))
                    }
                }
            }
        }
        observation
            .children
            .sort_by_key(|child| (child.kind == ChildKind::Process, child.name.clone()));
        observation
    }
    async fn processes(&self, target: &Resolved) -> Result<Vec<ChildObservation>> {
        let response = self
            .client
            .get(self.url(&["containers", &target.id, "top"]))
            .query(&[("ps_args", "-eo pid,ppid,stat,pcpu,rss,comm")])
            .send()
            .await?;
        let top: Value = serde_json::from_slice(&body(response).await?)?;
        parse_top(&target.epoch()?, &top)
    }
    async fn controller_rpc(
        &self,
        target: &Resolved,
        controller: &ControllerConfig,
        request: Value,
    ) -> Result<Value> {
        let kind = match controller.kind {
            ControllerKind::Supervisord => "supervisord",
            ControllerKind::Roslaunch => "roslaunch",
        };
        let created = self.json(Method::POST, &["containers",&target.id,"exec"], Some(json!({
            "AttachStdout":true,"AttachStderr":true,"Tty":false,
            "Cmd":[controller.python, "-c", CLIENT, kind, controller.socket, request.to_string()]
        }))).await?;
        let id = created["Id"].as_str().context("Docker omitted exec ID")?;
        let response = self
            .client
            .post(self.url(&["exec", id, "start"]))
            .header("content-type", "application/json")
            .body(json!({"Detach":false,"Tty":false}).to_string())
            .send()
            .await?;
        let output = demultiplex(&body(response).await?)?;
        let status = self.json(Method::GET, &["exec", id, "json"], None).await?;
        let reply: Value = serde_json::from_slice(&output)
            .context("Controller client did not return JSON; Python 3 with stdlib is required")?;
        ensure!(
            status["ExitCode"] == 0 && reply["ok"] == true,
            "Controller: {}",
            reply["error"]
                .as_str()
                .unwrap_or("RPC failed; outcome unknown")
        );
        Ok(reply["data"].clone())
    }
    async fn controller_children(
        &self,
        target: &Resolved,
        controller: &ControllerConfig,
    ) -> Result<Vec<ChildObservation>> {
        let reply = self
            .controller_rpc(target, controller, json!({"command":"snapshot"}))
            .await?;
        match controller.kind {
            ControllerKind::Roslaunch => {
                let mut children: Vec<ChildObservation> =
                    serde_json::from_value(reply["children"].clone())?;
                for child in &mut children {
                    child.epoch = format!("{}/{}", target.epoch()?, child.epoch);
                }
                Ok(children)
            }
            ControllerKind::Supervisord => supervisor_children(&target.epoch()?, &reply),
        }
    }
    pub async fn operate_child(
        &self,
        target: &Resolved,
        controller: &ControllerConfig,
        id: &str,
        epoch: &str,
        action: ChildAction,
    ) -> Result<()> {
        let current = self.controller_children(target, controller).await?;
        let child = current
            .iter()
            .find(|child| child.id == id)
            .context("Logical service no longer exists; refresh")?;
        ensure!(
            child.epoch == epoch,
            "Container/controller changed; refresh before operating"
        );
        ensure!(
            child.actions.contains(&action),
            "Operation unsupported: {}",
            child
                .reason
                .as_deref()
                .unwrap_or("current controller state")
        );
        let inner = epoch
            .strip_prefix(&format!("{}/", target.epoch()?))
            .context("invalid controller epoch")?;
        self.controller_rpc(
            target,
            controller,
            json!({"command":"operate","id":id,"epoch":inner,"action":action}),
        )
        .await
        .context("Controller operation failed; no retry or rollback was performed")?;
        Ok(())
    }
    pub async fn logs(&self, target: &Resolved) -> Result<String> {
        let response = self
            .client
            .get(self.url(&["containers", &target.id, "logs"]))
            .query(&[("stdout", "true"), ("stderr", "true"), ("tail", "150")])
            .send()
            .await?;
        let bytes = body(response).await?;
        let bytes = if target.info["Config"]["Tty"] == true {
            bytes
        } else {
            demultiplex(&bytes)?
        };
        Ok(String::from_utf8_lossy(&bytes).into_owned())
    }
}
fn negotiate_version(min: &str, max: &str) -> Result<String> {
    fn parse(s: &str) -> Result<(u16, u16)> {
        let (a, b) = s.split_once('.').context("invalid Docker API version")?;
        Ok((a.parse()?, b.parse()?))
    }
    let chosen = parse(min)?.max((1, 41));
    ensure!(
        chosen <= parse(max)? && chosen <= (1, 52),
        "Unsupported Docker API range {min}..{max}; client supports 1.41..1.52"
    );
    Ok(format!("v{}.{}", chosen.0, chosen.1))
}
async fn body(mut response: reqwest::Response) -> Result<Vec<u8>> {
    let status = response.status();
    let mut bytes = Vec::new();
    while let Some(chunk) = response.chunk().await? {
        ensure!(
            bytes.len() + chunk.len() <= LIMIT,
            "Docker response exceeds 4 MiB"
        );
        bytes.extend_from_slice(&chunk);
    }
    ensure!(
        !status.is_client_error() && !status.is_server_error(),
        "Docker {status}: {}",
        String::from_utf8_lossy(&bytes)
    );
    Ok(bytes)
}
pub fn demultiplex(bytes: &[u8]) -> Result<Vec<u8>> {
    let mut offset = 0;
    let mut output = Vec::new();
    while offset < bytes.len() {
        ensure!(bytes.len() - offset >= 8, "truncated Docker stream header");
        let header = &bytes[offset..offset + 8];
        ensure!(
            matches!(header[0], 1 | 2) && header[1..4] == [0, 0, 0],
            "invalid Docker stream header"
        );
        let len = u32::from_be_bytes(header[4..8].try_into()?) as usize;
        offset += 8;
        ensure!(len <= bytes.len() - offset, "truncated Docker stream frame");
        output.extend_from_slice(&bytes[offset..offset + len]);
        offset += len;
    }
    Ok(output)
}
pub fn parse_top(epoch: &str, top: &Value) -> Result<Vec<ChildObservation>> {
    let titles = top["Titles"]
        .as_array()
        .context("Docker omitted top columns")?;
    let index = |name: &str| titles.iter().position(|v| v.as_str() == Some(name));
    let pid = index("PID").context("Docker top has no host PID column")?;
    let mut children = Vec::new();
    for row in top["Processes"]
        .as_array()
        .context("Docker omitted process rows")?
    {
        let value = |column: Option<usize>| column.and_then(|i| row.get(i)).and_then(Value::as_str);
        let process: u32 = value(Some(pid)).context("missing PID")?.trim().parse()?;
        children.push(ChildObservation {
            id:format!("process:{process}"),epoch:epoch.into(),name:value(index("COMMAND")).unwrap_or("process").into(),kind:ChildKind::Process,parent:None,runtime_id:None,
            state:value(index("STAT")).unwrap_or("unknown").into(),pid:Some(process),pid_namespace:"Docker daemon host".into(),
            cpu_percent:value(index("%CPU")).and_then(|v|v.trim().parse().ok()),
            memory_bytes:value(index("RSS")).and_then(|v|v.trim().parse::<u64>().ok()).and_then(|n|n.checked_mul(1024)),
            exit_code:None,actions:vec![],reason:Some("OS process observation only; lifecycle actions require a logical controller service. CPU is ps lifetime average.".into()),
        });
    }
    Ok(children)
}
pub fn supervisor_children(container: &str, reply: &Value) -> Result<Vec<ChildObservation>> {
    let epoch = reply["epoch"]
        .as_str()
        .context("supervisord omitted identity")?;
    reply["processes"].as_array().context("supervisord omitted services")?.iter().map(|p|{
        let name=p["name"].as_str().context("missing service name")?;
        let group=p["group"].as_str().context("missing service group")?;
        let id=format!("{group}:{name}");ensure!(!id.contains('*'),"wildcard service names are unsupported");
        let state=p["statename"].as_str().unwrap_or("UNKNOWN");
        let actions=match state {"RUNNING"|"STARTING"|"BACKOFF"=>vec![ChildAction::Stop,ChildAction::Restart],"STOPPED"|"EXITED"|"FATAL"=>vec![ChildAction::Start,ChildAction::Restart],_=>vec![]};
        Ok(ChildObservation{id,epoch:format!("{container}/{epoch}"),name:format!("{group}:{name}"),kind:ChildKind::Service,parent:None,runtime_id:None,state:state.into(),pid:p["pid"].as_u64().filter(|p|*p>0).and_then(|p|u32::try_from(p).ok()),pid_namespace:"container".into(),cpu_percent:None,memory_bytes:None,exit_code:p["exitstatus"].as_i64().and_then(|n|i32::try_from(n).ok()),actions,reason:Some("supervisord owns lifecycle; PID is container-local. Per-process resources are listed separately; no PID namespace guessing.".into())})
    }).collect()
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn health_fallback_does_not_aggregate_children() {
        assert!(is_ready(&json!({"State":{"Running":true}})));
        assert!(!is_ready(&json!({"State":{"Running":true,"Paused":true}})));
        assert!(!is_ready(
            &json!({"State":{"Running":true,"Restarting":true}})
        ));
        assert!(!is_ready(
            &json!({"State":{"Running":true,"Health":{"Status":"unhealthy"}}})
        ));
        assert!(is_ready(
            &json!({"State":{"Running":true,"Health":{"Status":"healthy"}}})
        ));
    }
    #[test]
    fn api_range_and_multiplexed_errors() {
        assert_eq!(negotiate_version("1.44", "1.52").unwrap(), "v1.44");
        assert_eq!(negotiate_version("1.24", "1.43").unwrap(), "v1.41");
        assert!(negotiate_version("1.53", "1.53").is_err());
        assert!(demultiplex(&[1, 0, 0, 0, 0, 0, 0, 2, b'x']).is_err());
        assert_eq!(
            demultiplex(&[1, 0, 0, 0, 0, 0, 0, 1, b'x', 2, 0, 0, 0, 0, 0, 0, 1, b'y']).unwrap(),
            b"xy"
        );
    }
    #[test]
    fn top_has_observation_only_capabilities_and_stopped_services_keep_identity() {
        let rows=parse_top("container",&json!({"Titles":["PID","COMMAND","RSS","%CPU","STAT"],"Processes":[["42","sleep","100","2.5","S"]]})).unwrap();
        assert!(rows[0].actions.is_empty());
        assert_eq!(rows[0].memory_bytes, Some(102400));
        let s=supervisor_children("container",&json!({"epoch":"1","processes":[{"group":"robot","name":"camera","statename":"STOPPED","pid":0,"exitstatus":0}]})).unwrap();
        assert_eq!(s[0].id, "robot:camera");
        assert_eq!(s[0].pid, None);
        assert!(s[0].actions.contains(&ChildAction::Start));
    }
}
