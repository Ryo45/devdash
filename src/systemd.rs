use crate::{
    engine::{Action, Backend, Observation},
    inspection::{DependencyGraph, Relation},
    model::*,
    monitor::{BackendEvent, ManagerHealth},
    systemd_events::{ManagerEvents, start_reader},
};
use anyhow::{Context, Result, bail, ensure};
use async_trait::async_trait;
use futures_util::StreamExt;
use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};
use tokio::sync::broadcast;
use zbus::{Connection, Proxy, proxy::MethodFlags, zvariant::OwnedObjectPath};

// One immutable connection generation. An operation keeps this Arc until its
// result is known: reconnecting never moves an old request to the new manager.
struct ManagerSession {
    control: Connection,
    owner: zbus::names::OwnedUniqueName,
    tracker: Arc<ManagerEvents>,
    generation: u64,
    canonical: Mutex<BTreeMap<UnitKey, UnitKey>>,
    _reader: tokio_util::task::AbortOnDropHandle<()>,
}
#[derive(Default)]
struct ManagerSlot {
    session: Option<Arc<ManagerSession>>,
    error: Option<String>,
}
pub struct Systemd {
    managers: BTreeMap<Scope, Arc<Mutex<ManagerSlot>>>,
    events: broadcast::Sender<BackendEvent>,
    _reconnectors: Vec<tokio_util::task::AbortOnDropHandle<()>>,
    revision: Arc<AtomicU64>,
}
impl Systemd {
    pub async fn connect() -> Result<Self> {
        let (events, _) = broadcast::channel(256);
        let revision = Arc::new(AtomicU64::new(0));
        let mut managers = BTreeMap::new();
        let mut reconnectors = Vec::new();
        for scope in [Scope::User, Scope::System] {
            let initial = connect_manager(scope, revision.clone(), events.clone()).await;
            let slot = Arc::new(Mutex::new(ManagerSlot::default()));
            match initial {
                Ok(session) => slot.lock().unwrap().session = Some(session),
                Err(error) => slot.lock().unwrap().error = Some(format!("{error:#}")),
            }
            managers.insert(scope, slot.clone());
            reconnectors.push(tokio_util::task::AbortOnDropHandle::new(tokio::spawn(
                reconnect(scope, slot, revision.clone(), events.clone()),
            )));
        }
        // A temporarily unavailable manager must not prevent the GUI/Core from
        // starting. Its health is reported, and only that scope rejects requests.
        Ok(Self {
            managers,
            events,
            _reconnectors: reconnectors,
            revision,
        })
    }
    fn session(&self, scope: Scope) -> Result<Arc<ManagerSession>> {
        let slot = self.managers[&scope].lock().unwrap();
        let session = slot.session.as_ref().ok_or_else(|| {
            anyhow::anyhow!(
                "{scope} manager unavailable; reconnecting: {}",
                slot.error.as_deref().unwrap_or("connecting")
            )
        })?;
        session.tracker.ensure_available()?;
        Ok(session.clone())
    }
}
impl ManagerSession {
    async fn manager(&self) -> Result<Proxy<'static>> {
        Ok(Proxy::new_owned(
            self.control.clone(),
            self.owner.clone(),
            "/org/freedesktop/systemd1",
            "org.freedesktop.systemd1.Manager",
        )
        .await?)
    }
    async fn unit(&self, key: &UnitKey) -> Result<Proxy<'static>> {
        let path: OwnedObjectPath = self
            .manager()
            .await?
            .call("LoadUnit", &(key.unit.as_str(),))
            .await?;
        read_proxy(self, path, "org.freedesktop.systemd1.Unit").await
    }
}
async fn connect_manager(
    scope: Scope,
    revision: Arc<AtomicU64>,
    events: broadcast::Sender<BackendEvent>,
) -> Result<Arc<ManagerSession>> {
    tokio::time::timeout(Duration::from_secs(5), async {
        let control = bus(scope).await?;
        let observer = bus(scope).await?;
        let dbus = zbus::fdo::DBusProxy::new(&control).await?;
        let owner = dbus
            .get_name_owner("org.freedesktop.systemd1".try_into()?)
            .await?;
        let tracker = ManagerEvents::new(scope, revision.clone(), events);
        let reader = start_reader(
            observer.clone(),
            control.clone(),
            owner.clone(),
            tracker.clone(),
        )
        .await?;
        for connection in [&observer, &control] {
            let manager = Proxy::new(
                connection,
                owner.as_str(),
                "/org/freedesktop/systemd1",
                "org.freedesktop.systemd1.Manager",
            )
            .await?;
            let _: () = manager.call("Subscribe", &()).await?;
        }
        // An owner change between resolution and subscription invalidates setup.
        ensure!(
            dbus.get_name_owner("org.freedesktop.systemd1".try_into()?)
                .await?
                == owner,
            "manager changed during subscription"
        );
        tracker.ensure_available()?;
        let generation = revision.fetch_add(1, Ordering::SeqCst) + 1;
        Ok(Arc::new(ManagerSession {
            control,
            owner,
            tracker,
            generation,
            canonical: Mutex::new(BTreeMap::new()),
            _reader: reader,
        }))
    })
    .await
    .context("Connection/Subscribe timed out")?
}
async fn reconnect(
    scope: Scope,
    slot: Arc<Mutex<ManagerSlot>>,
    revision: Arc<AtomicU64>,
    events: broadcast::Sender<BackendEvent>,
) {
    let mut delay = Duration::from_secs(1);
    loop {
        let session = slot.lock().unwrap().session.clone();
        if let Some(session) = session {
            session.tracker.lost.cancelled().await;
            let mut state = slot.lock().unwrap();
            state.error = session.tracker.health().reason;
            state.session = None;
        }
        tokio::time::sleep(delay).await;
        match connect_manager(scope, revision.clone(), events.clone()).await {
            Ok(session) => {
                let generation = session.generation;
                {
                    let mut state = slot.lock().unwrap();
                    state.session = Some(session);
                    state.error = None;
                }
                let _ = events.send(BackendEvent::new(scope, None,
                    crate::monitor::EventKind::Reconnected,
                    format!("{scope} manager monitoring restored (generation {generation}); history gap; interrupted operations are not resumed")));
                delay = Duration::from_secs(1);
            }
            Err(error) => {
                slot.lock().unwrap().error = Some(format!("{error:#}"));
                delay = (delay * 2).min(Duration::from_secs(10));
            }
        }
    }
}
impl Systemd {
    async fn operate_session(
        session: Arc<ManagerSession>,
        w: &Workload,
        action: Action,
    ) -> Result<()> {
        let tracker = &session.tracker;
        let key = UnitKey::from(w);
        // Preparation has no lifecycle request to preserve. Stop waiting on
        // metadata/match-rule replies as soon as this generation is lost.
        let prepare = async {
            let manager = session.manager().await?;
            let canonical: String = session.unit(&key).await?.get_property("Id").await?;
            let canonical_key = UnitKey::new(w.scope, canonical.clone());
            session
                .canonical
                .lock()
                .unwrap()
                .insert(key, canonical_key.clone());
            let removed = manager
                .receive_signal_with_args("JobRemoved", &[(2, canonical.as_str())])
                .await?;
            Ok::<_, anyhow::Error>((manager, canonical, canonical_key, removed))
        };
        let (manager, canonical, canonical_key, mut removed) = tokio::select! {
            biased;
            _=tracker.lost.cancelled()=>bail!("{}:{}: manager lost during preparation; outcome unknown",w.scope,w.unit),
            result=prepare=>result?,
        };
        let mut request = tracker.begin(canonical_key)?;
        let method = match action {
            Action::Start => "StartUnit",
            Action::Stop => "StopUnit",
            Action::Restart => "RestartUnit",
            Action::Reinitialize => bail!("reinitialize requires a group"),
        };
        let body = (w.unit.as_str(), "replace");
        let call = manager.call_with_flags::<_, _, OwnedObjectPath>(
            method,
            MethodFlags::AllowInteractiveAuth.into(),
            &body,
        );
        tokio::pin!(call);
        // Drain early completions while the method reply is pending. In particular,
        // a delayed reply cannot fill this operation's signal queue.
        let mut early = BTreeMap::new();
        let path = loop {
            tokio::select! {
                biased;
                _=tracker.lost.cancelled()=>bail!("{}:{canonical}: manager owner/connection lost; outcome unknown",w.scope),
                reply=&mut call=>break reply.map_err(|e|operation_error(w.scope,&w.unit,action,e))?.context("systemd omitted the job reply")?,
                message=removed.next()=>{
                    let message=message.context("job stream ended; outcome unknown")?;
                    let (_,job,_,result):(u32,OwnedObjectPath,String,String)=message.body().deserialize()?;
                    ensure!(early.len()<256,"job result buffer overflow; outcome unknown");early.insert(job.to_string(),result);
                }
            }
        };
        request.register(path.as_str());
        if let Some(result) = early.remove(path.as_str()) {
            ensure!(
                result == "done",
                "{method} {}:{canonical}: systemd job result {result}",
                w.scope
            );
            return Ok(());
        }
        loop {
            tokio::select! {
                biased;
                _=tracker.lost.cancelled()=>bail!("{}:{canonical}: manager owner/connection lost; outcome unknown",w.scope),
                message=removed.next()=>{
                    let message=message.context("job stream ended; outcome unknown")?;
                    let (_,job,unit,result):(u32,OwnedObjectPath,String,String)=message.body().deserialize()?;
                    if job==path{ensure!(result=="done","{method} {}:{unit}: systemd job result {result}",w.scope);return Ok(());}
                }
            }
        }
    }
}

async fn bus(scope: Scope) -> Result<Connection> {
    Ok(match scope {
        Scope::User => Connection::session().await?,
        Scope::System => Connection::system().await?,
    })
}
// No lazy property subscriptions. All reads stay pinned to one manager owner.
async fn read_proxy(
    session: &ManagerSession,
    path: OwnedObjectPath,
    interface: &'static str,
) -> Result<Proxy<'static>> {
    Ok(
        zbus::proxy::Builder::<Proxy<'static>>::new(&session.control)
            .destination(session.owner.clone())?
            .path(path)?
            .interface(interface)?
            .cache_properties(zbus::proxy::CacheProperties::No)
            .build()
            .await?,
    )
}
pub fn operation_error(
    scope: Scope,
    unit: &str,
    action: Action,
    error: zbus::Error,
) -> anyhow::Error {
    let name = match &error {
        zbus::Error::MethodError(name, _, _) => name.as_str(),
        _ => "",
    };
    if name.contains("AccessDenied") || name.contains("Auth") || name.contains("PermissionDenied") {
        anyhow::anyhow!(
            "Authorization failed for {scope}:{unit} {action:?}. systemd/Polkit must authorize org.freedesktop.systemd1.manage-units; check the session authentication agent and policy. {error}"
        )
    } else {
        anyhow::anyhow!("{scope}:{unit} {action:?}: {error}")
    }
}
#[async_trait]
impl Backend for Systemd {
    async fn operate(&self, w: &Workload, action: Action) -> Result<()> {
        let session = self.session(w.scope)?;
        Self::operate_session(session, w, action).await
    }
    async fn operate_guarded(
        &self,
        w: &Workload,
        action: Action,
        loss: Option<&tokio_util::sync::CancellationToken>,
    ) -> Result<()> {
        // Capture first, then check the run's old generation. A replacement can
        // never receive the next step of an interrupted sequence.
        let session = self.session(w.scope)?;
        ensure!(
            !loss.is_some_and(tokio_util::sync::CancellationToken::is_cancelled),
            "{} manager connection lost; outcome unknown; sequence will not resume",
            w.scope
        );
        Self::operate_session(session, w, action).await
    }
    fn operation_loss(&self, scope: Scope) -> Option<tokio_util::sync::CancellationToken> {
        Some(match self.session(scope) {
            Ok(session) => session.tracker.lost.clone(),
            Err(_) => {
                let token = tokio_util::sync::CancellationToken::new();
                token.cancel();
                token
            }
        })
    }
    async fn observe(&self, w: &Workload) -> Result<Observation> {
        let session = self.session(w.scope)?;
        let key = UnitKey::from(w);
        let unit=match session.unit(&key).await {
            Ok(p)=>p,
            Err(e) if e.downcast_ref::<zbus::Error>().is_some_and(|e|matches!(e,zbus::Error::MethodError(name,_,_) if name.as_str()=="org.freedesktop.systemd1.NoSuchUnit"))=>return Ok(Observation{state:"not_found".into(),manager_generation:Some(session.generation),..Default::default()}),
            Err(e)=>return Err(e),
        };
        let load: String = unit.get_property("LoadState").await?;
        if load == "not-found" {
            return Ok(Observation {
                state: "not_found".into(),
                manager_generation: Some(session.generation),
                ..Default::default()
            });
        }
        let state = unit.get_property("ActiveState").await?;
        let sub_state = unit.get_property("SubState").await?;
        let canonical_unit: Option<String> = unit.get_property("Id").await.ok();
        if let Some(name) = &canonical_unit {
            session
                .canonical
                .lock()
                .unwrap()
                .insert(key, UnitKey::new(w.scope, name.clone()));
        }
        let invocation_id = unit
            .get_property::<Vec<u8>>("InvocationID")
            .await
            .ok()
            .filter(|v| v.iter().any(|b| *b != 0))
            .map(|v| v.iter().map(|b| format!("{b:02x}")).collect());
        let service = read_proxy(
            &session,
            unit.path().to_owned().into(),
            "org.freedesktop.systemd1.Service",
        )
        .await?;
        let valid = |v: Option<u64>| v.filter(|n| *n != u64::MAX);
        session.tracker.ensure_available()?;
        Ok(Observation {
            manager_generation: Some(session.generation),
            state,
            sub_state,
            canonical_unit,
            invocation_id,
            pid: service
                .get_property::<u32>("MainPID")
                .await
                .ok()
                .filter(|n| *n != 0),
            memory_bytes: valid(service.get_property("MemoryCurrent").await.ok()),
            cpu_nsec: valid(service.get_property("CPUUsageNSec").await.ok()),
            error: None,
            ..Default::default()
        })
    }
    async fn logs(&self, w: &Workload) -> Result<String> {
        logs(w).await
    }
    async fn scan(&self, scope: Scope) -> Result<Vec<String>> {
        tokio::time::timeout(Duration::from_secs(5), async {
            let session = self.session(scope)?;
            let manager = session.manager().await?;
            let files: Vec<(String, String)> = manager.call("ListUnitFiles", &()).await?;
            let mut names: BTreeSet<_> = files
                .into_iter()
                .filter_map(|(p, _)| {
                    p.rsplit('/')
                        .next()
                        .filter(|s| s.ends_with(".service"))
                        .map(str::to_string)
                })
                .collect();
            type Row = (
                String,
                String,
                String,
                String,
                String,
                String,
                OwnedObjectPath,
                u32,
                String,
                OwnedObjectPath,
            );
            let loaded: Vec<Row> = manager.call("ListUnits", &()).await?;
            names.extend(
                loaded
                    .into_iter()
                    .map(|r| r.0)
                    .filter(|n| n.ends_with(".service")),
            );
            Ok(names.into_iter().collect())
        })
        .await
        .with_context(|| format!("{scope} scan timed out"))?
    }
    fn subscribe_events(&self) -> Option<broadcast::Receiver<BackendEvent>> {
        Some(self.events.subscribe())
    }
    fn recently_requested(&self, key: &UnitKey) -> bool {
        let Ok(session) = self.session(key.scope) else {
            return false;
        };
        let canonical = session
            .canonical
            .lock()
            .unwrap()
            .get(key)
            .cloned()
            .unwrap_or_else(|| key.clone());
        session.tracker.recently_requested(&canonical)
    }
    fn inspection_revision(&self) -> u64 {
        self.revision.load(Ordering::SeqCst)
    }
    fn monitoring_health(&self) -> BTreeMap<Scope, ManagerHealth> {
        self.managers
            .iter()
            .map(|(scope, slot)| {
                let state = slot.lock().unwrap();
                let health = state
                    .session
                    .as_ref()
                    .map(|s| s.tracker.health())
                    .unwrap_or_else(|| ManagerHealth {
                        available: false,
                        reason: state.error.clone().or_else(|| Some("connecting".into())),
                    });
                (*scope, health)
            })
            .collect()
    }
    async fn dependencies(&self, seeds: &[UnitKey]) -> Result<DependencyGraph> {
        let mut graph = DependencyGraph::default();
        let sessions: BTreeMap<_, _> = seeds
            .iter()
            .map(|s| s.scope)
            .collect::<BTreeSet<_>>()
            .into_iter()
            .filter_map(|scope| match self.session(scope) {
                Ok(session) => Some((scope, session)),
                Err(error) => {
                    graph.incomplete.push(format!("{error:#}"));
                    None
                }
            })
            .collect();
        let mut pending: VecDeque<_> = seeds.iter().cloned().collect();
        let mut visited = BTreeSet::new();
        let deadline = Instant::now() + Duration::from_secs(6);
        while let Some(key) = pending.pop_front() {
            if !visited.insert(key.clone()) {
                continue;
            }
            if visited.len() > 256 || Instant::now() > deadline {
                graph.incomplete.push("Inspection bounded at 256 unit lookups / 6 seconds; additional dependencies were not inspected".into());
                break;
            }
            let result = tokio::time::timeout(
                deadline
                    .saturating_duration_since(Instant::now())
                    .min(Duration::from_secs(2)),
                async {
                    let session = sessions.get(&key.scope).context("manager unavailable")?;
                    read_dependencies(session, &key, &mut graph, &mut pending).await
                },
            )
            .await;
            match result {
                Ok(Ok(())) => {}
                Ok(Err(e)) => graph.incomplete.push(format!("{key}: {e:#}")),
                Err(_) => graph
                    .incomplete
                    .push(format!("{key}: dependency lookup timed out")),
            }
        }
        for scope in seeds.iter().map(|s| s.scope).collect::<BTreeSet<_>>() {
            if let Some(session) = sessions.get(&scope) {
                let tracker = &session.tracker;
                if tracker.reloading.load(Ordering::SeqCst) {
                    graph.stale = true;
                    graph
                        .incomplete
                        .push(format!("{scope} manager is reloading"));
                }
                if !tracker.health().available {
                    graph.incomplete.push(format!("{scope} event monitoring unavailable; future invalidations cannot be tracked"));
                }
            }
        }
        Ok(graph)
    }
}
async fn read_dependencies(
    session: &ManagerSession,
    key: &UnitKey,
    graph: &mut DependencyGraph,
    pending: &mut VecDeque<UnitKey>,
) -> Result<()> {
    use Relation::*;
    let unit = session.unit(key).await?;
    let id: String = unit.get_property("Id").await?;
    let canonical = UnitKey::new(key.scope, id);
    graph.aliases.insert(key.clone(), canonical.clone());
    if graph.inspected.contains(&canonical) {
        return Ok(());
    }
    let load: String = unit.get_property("LoadState").await?;
    ensure!(load == "loaded", "LoadState={load}");
    // Read GetAll once: no file parsing and no cached dependency snapshots across checks.
    let properties = zbus::fdo::PropertiesProxy::builder(&session.control)
        .destination(session.owner.clone())?
        .path(unit.path().to_owned())?
        .build()
        .await?;
    let values = properties
        .get_all("org.freedesktop.systemd1.Unit".try_into()?)
        .await?;
    for (property, kind, inverse, follow) in [
        ("Wants", Wants, false, true),
        ("Requires", Requires, false, true),
        ("BindsTo", BindsTo, false, true),
        ("PartOf", PartOf, false, false),
        ("RequiredBy", Requires, true, true),
        ("WantedBy", Wants, true, false),
        ("BoundBy", BindsTo, true, true),
        ("ConsistsOf", PartOf, true, true),
        ("After", After, false, false),
        ("Before", Before, false, false),
        ("Conflicts", Conflicts, false, false),
        ("ConflictedBy", Conflicts, true, false),
        ("Requisite", Requisite, false, false),
        ("RequisiteOf", Requisite, true, true),
        ("Upholds", Upholds, false, true),
        ("UpheldBy", Upholds, true, true),
        ("PropagatesStopTo", PropagatesStopTo, false, true),
        ("StopPropagatedFrom", PropagatesStopTo, true, false),
    ] {
        if let Some(value) = values.get(property) {
            let names: Vec<String> = value.try_clone()?.try_into()?;
            for name in names {
                let other = UnitKey::new(key.scope, name);
                if follow {
                    pending.push_back(other.clone());
                }
                if inverse {
                    graph.edge(other, canonical.clone(), kind);
                } else {
                    graph.edge(canonical.clone(), other, kind);
                }
            }
        } else if matches!(
            property,
            "Wants"
                | "Requires"
                | "BindsTo"
                | "PartOf"
                | "After"
                | "Before"
                | "Conflicts"
                | "RequiredBy"
                | "BoundBy"
                | "ConsistsOf"
        ) {
            graph
                .incomplete
                .push(format!("{canonical}: property {property} unavailable"));
        }
    }
    for property in ["OnFailure", "OnSuccess", "Triggers", "TriggeredBy"] {
        if let Some(value) = values.get(property) {
            let names: Vec<String> = value.try_clone()?.try_into()?;
            if !names.is_empty() {
                graph.incomplete.push(format!(
                    "{canonical}: {property}={names:?} is not simulated"
                ));
            }
        }
    }
    graph.inspected.insert(canonical);
    Ok(())
}
pub async fn logs(w: &Workload) -> Result<String> {
    let mut cmd = tokio::process::Command::new("journalctl");
    cmd.arg(match w.scope {
        Scope::User => "--user",
        Scope::System => "--system",
    });
    let output = tokio::time::timeout(
        Duration::from_secs(5),
        cmd.kill_on_drop(true)
            .args([
                "--no-pager",
                "-n",
                "150",
                "-o",
                "short-iso",
                "--unit",
                &w.unit,
            ])
            .output(),
    )
    .await??;
    ensure!(
        output.status.success(),
        "journalctl: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let mut logs = String::new();
    if !output.stderr.is_empty() {
        logs.push_str(&String::from_utf8_lossy(&output.stderr));
        logs.push('\n');
    }
    logs.push_str(&String::from_utf8_lossy(&output.stdout));
    Ok(logs)
}
