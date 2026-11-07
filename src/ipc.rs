use crate::{
    engine::{Action, Engine, Observation, Run},
    inspection::InspectionReport,
    model::{Config, Scope},
    monitor::{Event, ManagerHealth},
};
use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::{Path, PathBuf},
    sync::Arc,
};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    net::{UnixListener, UnixStream},
    sync::RwLock,
};
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "command", rename_all = "snake_case")]
pub enum Request {
    Snapshot,
    Operate {
        target: String,
        group: bool,
        action: Action,
    },
    OperateChild {
        parent: String,
        child: String,
        epoch: String,
        action: Action,
    },
    Approve {
        gate: String,
    },
    Cancel,
    Scan {
        #[serde(default)]
        scope: Scope,
    },
    Import {
        unit: String,
        #[serde(default)]
        scope: Scope,
    },
    Inspect {
        group: String,
        action: Action,
    },
    Logs {
        workload: String,
    },
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Snapshot {
    pub config: Config,
    pub run: Run,
    pub approved: BTreeSet<String>,
    pub observations: BTreeMap<String, Observation>,
    pub inspections: BTreeMap<String, InspectionReport>,
    pub events: Vec<Event>,
    pub monitoring: BTreeMap<Scope, ManagerHealth>,
    pub monitor_gap: Option<String>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Response {
    pub ok: bool,
    pub error: Option<String>,
    pub data: serde_json::Value,
}
impl Response {
    fn success(v: impl Serialize) -> Self {
        Self {
            ok: true,
            error: None,
            data: serde_json::to_value(v).unwrap(),
        }
    }
    fn error(e: anyhow::Error) -> Self {
        Self {
            ok: false,
            error: Some(format!("{e:#}")),
            data: serde_json::Value::Null,
        }
    }
}
pub fn socket_path() -> PathBuf {
    std::env::var_os("XDG_RUNTIME_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(format!("/run/user/{}", {
                use std::os::unix::fs::MetadataExt;
                std::fs::metadata("/proc/self")
                    .map(|m| m.uid())
                    .unwrap_or(u32::MAX)
            }))
        })
        .join("devdash/core.sock")
}
pub async fn request(path: &Path, request: Request) -> Result<Response> {
    let stream = UnixStream::connect(path).await?;
    let (read, mut write) = stream.into_split();
    write.write_all(&serde_json::to_vec(&request)?).await?;
    write.write_all(b"\n").await?;
    let mut line = String::new();
    BufReader::new(read.take_limit())
        .read_line(&mut line)
        .await?;
    ensure!(!line.is_empty(), "core disconnected");
    let r: Response = serde_json::from_str(&line)?;
    ensure!(r.ok, "{}", r.error.as_deref().unwrap_or("request failed"));
    Ok(r)
}
// Bound messages to prevent a malformed local client from exhausting memory.
trait LimitedRead: tokio::io::AsyncRead + Sized {
    fn take_limit(self) -> tokio::io::Take<Self> {
        tokio::io::AsyncReadExt::take(self, 4 * 1024 * 1024)
    }
}
impl<T: tokio::io::AsyncRead + Sized> LimitedRead for T {}
pub async fn serve(engine: Arc<Engine>, socket: &Path, config_path: PathBuf) -> Result<()> {
    use std::os::unix::fs::{DirBuilderExt, FileTypeExt, OpenOptionsExt, PermissionsExt};
    let dir = socket
        .parent()
        .ok_or_else(|| anyhow::anyhow!("socket requires parent directory"))?;
    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(dir)?;
    let lock = std::fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .mode(0o600)
        .open(socket.with_extension("lock"))?;
    lock.try_lock().context("another core owns this socket")?;
    if let Ok(meta) = std::fs::symlink_metadata(socket) {
        ensure!(meta.file_type().is_socket(), "socket path is not a socket");
        ensure!(
            UnixStream::connect(socket).await.is_err(),
            "another server is listening"
        );
        std::fs::remove_file(socket)?;
    }
    let listener = UnixListener::bind(socket)?;
    std::fs::set_permissions(socket, std::fs::Permissions::from_mode(0o600))?;
    let _guard = SocketGuard {
        path: socket.to_path_buf(),
        _lock: lock,
    };
    // Capture events before taking the initial baseline. Initialization events are
    // buffered, so a start/stop during the first state query is not silently lost.
    let events = engine.backend.subscribe_events();
    let baseline_started = std::time::Instant::now();
    let initial = engine.observations().await;
    engine.monitor.lock().unwrap().update(
        &engine.config,
        &engine.snapshot(),
        &engine.approved(),
        initial.clone(),
        &BTreeSet::new(),
    );
    let observations = Arc::new(RwLock::new(initial));
    let monitor = tokio_util::task::AbortOnDropHandle::new(tokio::spawn(crate::monitor::watch(
        engine.clone(),
        observations.clone(),
        events,
        baseline_started,
    )));
    let imports = Arc::new(tokio::sync::Mutex::new(()));
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    loop {
        let stream = tokio::select! {r=listener.accept()=>r?.0,_=tokio::signal::ctrl_c()=>break,_=terminate.recv()=>break};
        let e = engine.clone();
        let obs = observations.clone();
        let config_path = config_path.clone();
        let imports = imports.clone();
        tokio::spawn(async move {
            let (read, mut write) = stream.into_split();
            let mut line = String::new();
            let reply = async {
                tokio::time::timeout(
                    std::time::Duration::from_secs(10),
                    BufReader::new(read.take_limit()).read_line(&mut line),
                )
                .await??;
                let req: Request = serde_json::from_str(&line)?;
                Ok::<_, anyhow::Error>(match req {
                    Request::Snapshot => {
                        let (events, monitor_gap) = {
                            let monitor = e.monitor.lock().unwrap();
                            (monitor.events(), monitor.stream_gap.clone())
                        };
                        Response::success(Snapshot {
                            config: (*e.config).clone(),
                            run: e.snapshot(),
                            approved: e.approved(),
                            observations: obs.read().await.clone(),
                            inspections: e.reports(),
                            events,
                            monitoring: e.backend.monitoring_health(),
                            monitor_gap,
                        })
                    }
                    Request::Operate {
                        target,
                        group,
                        action,
                    } => {
                        e.launch(target, group, action)?;
                        Response::success("accepted")
                    }
                    Request::OperateChild {
                        parent,
                        child,
                        epoch,
                        action,
                    } => {
                        e.launch_child(parent, child, epoch, action)?;
                        Response::success("accepted")
                    }
                    Request::Approve { gate } => {
                        e.approve(&gate)?;
                        Response::success("approved")
                    }
                    Request::Cancel => {
                        e.cancel()?;
                        Response::success("cancelling")
                    }
                    Request::Scan { scope } => Response::success(e.backend.scan(scope).await?),
                    Request::Inspect { group, action } => {
                        Response::success(e.inspect(&group, action).await?)
                    }
                    Request::Logs { workload } => {
                        let w = e
                            .config
                            .workloads
                            .get(&workload)
                            .ok_or_else(|| anyhow::anyhow!("unknown workload"))?;
                        Response::success(e.backend.logs(w).await?)
                    }
                    Request::Import { unit, scope } => {
                        let _guard = imports.lock().await;
                        ensure!(
                            e.backend.scan(scope).await?.contains(&unit),
                            "unit not found in scan"
                        );
                        let mut cfg = Config::load(&config_path)?;
                        let id = cfg.import(&unit, scope)?;
                        let backup = config_path.with_extension("yaml.bak");
                        std::fs::copy(&config_path, &backup)?;
                        let tmp = config_path.with_extension("yaml.tmp");
                        std::fs::write(&tmp, serde_yaml::to_string(&cfg)?)?;
                        std::fs::rename(tmp, &config_path)?;
                        Response::success(format!(
                            "Imported {id}. Restart core to load the edited config; backup: {}",
                            backup.display()
                        ))
                    }
                })
            }
            .await;
            let reply = reply.unwrap_or_else(Response::error);
            if let Ok(bytes) = serde_json::to_vec(&reply) {
                let _ = write.write_all(&bytes).await;
                let _ = write.write_all(b"\n").await;
            }
        });
    }
    monitor.abort();
    Ok(())
}

struct SocketGuard {
    path: PathBuf,
    _lock: std::fs::File,
}
impl Drop for SocketGuard {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.path);
    }
}
