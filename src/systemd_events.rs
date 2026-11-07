//! Per-manager event ownership and request correlation. No service supervision.
use crate::{
    model::{Scope, UnitKey},
    monitor::{BackendEvent, Correlation, EventKind, ManagerHealth},
};
use anyhow::{Result, ensure};
use futures_util::StreamExt;
use std::{
    collections::BTreeMap,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};
use tokio::sync::{broadcast, oneshot};
use tokio_util::{sync::CancellationToken, task::AbortOnDropHandle};
use zbus::{Connection, Proxy};

#[derive(Default)]
struct Requests {
    issuing: BTreeMap<UnitKey, usize>,
    owned: BTreeMap<String, UnitKey>,
    early: BTreeMap<String, BackendEvent>,
    recent: BTreeMap<UnitKey, Instant>,
    failure: Option<String>,
}
pub(crate) struct ManagerEvents {
    scope: Scope,
    state: Mutex<Requests>,
    pub lost: CancellationToken,
    pub reloading: AtomicBool,
    revision: Arc<AtomicU64>,
    sender: broadcast::Sender<BackendEvent>,
}
impl ManagerEvents {
    pub fn new(
        scope: Scope,
        revision: Arc<AtomicU64>,
        sender: broadcast::Sender<BackendEvent>,
    ) -> Arc<Self> {
        Arc::new(Self {
            scope,
            state: Mutex::new(Requests::default()),
            lost: CancellationToken::new(),
            reloading: AtomicBool::new(false),
            revision,
            sender,
        })
    }
    pub fn health(&self) -> ManagerHealth {
        ManagerHealth {
            available: !self.lost.is_cancelled(),
            reason: self.state.lock().unwrap().failure.clone(),
        }
    }
    pub fn ensure_available(&self) -> Result<()> {
        let h = self.health();
        ensure!(
            h.available,
            "{} event tracking unavailable: {}; outcome unknown; reconnecting",
            self.scope,
            h.reason.as_deref().unwrap_or("manager unavailable")
        );
        Ok(())
    }
    pub fn begin(self: &Arc<Self>, unit: UnitKey) -> Result<RequestGuard> {
        self.ensure_available()?;
        *self
            .state
            .lock()
            .unwrap()
            .issuing
            .entry(unit.clone())
            .or_default() += 1;
        Ok(RequestGuard {
            tracker: self.clone(),
            unit,
            registered: false,
        })
    }
    pub fn recently_requested(&self, key: &UnitKey) -> bool {
        let state = self.state.lock().unwrap();
        state.issuing.contains_key(key)
            || state.owned.values().any(|u| u == key)
            || state
                .recent
                .get(key)
                .is_some_and(|t| t.elapsed() < Duration::from_secs(5))
    }
    pub fn emit(&self, event: BackendEvent) {
        let _ = self.sender.send(event);
    }
    pub fn fail(&self, reason: String) {
        {
            let mut state = self.state.lock().unwrap();
            if state.failure.is_some() {
                return;
            }
            state.failure = Some(reason.clone());
            state.owned.clear();
            state.early.clear();
        }
        self.revision.fetch_add(1, Ordering::SeqCst);
        self.lost.cancel();
        self.emit(BackendEvent::new(
            self.scope,
            None,
            EventKind::Disconnected,
            format!(
                "{} manager event tracking lost: {reason}; pending outcomes unknown",
                self.scope
            ),
        ));
    }
    fn job(&self, mut event: BackendEvent) {
        let path = event.job_path.as_ref().unwrap().clone();
        let mut state = self.state.lock().unwrap();
        if self.lost.is_cancelled() {
            return;
        }
        if let Some(unit) = state.owned.remove(&path) {
            state.recent.insert(unit, Instant::now());
            event.correlation = Correlation::Requested;
        } else if event
            .unit
            .as_ref()
            .is_some_and(|u| state.issuing.contains_key(u))
        {
            if state.early.len() >= 256 {
                drop(state);
                self.fail("correlation buffer overflow; event history incomplete".into());
                return;
            }
            state.early.insert(path, event);
            return;
        } else {
            event.correlation = Correlation::Unrelated;
        }
        drop(state);
        self.emit(event);
    }
    fn finish_issuing(&self, unit: &UnitKey, path: Option<&str>) {
        let reply_known = path.is_some();
        let mut state = self.state.lock().unwrap();
        let mut publish = Vec::new();
        if let Some(path) = path
            && !self.lost.is_cancelled()
        {
            state
                .recent
                .retain(|_, time| time.elapsed() < Duration::from_secs(5));
            state.recent.insert(unit.clone(), Instant::now());
            if let Some(mut event) = state.early.remove(path) {
                event.correlation = Correlation::Requested;
                publish.push(event);
            } else {
                state.owned.insert(path.into(), unit.clone());
            }
        }
        if let Some(count) = state.issuing.get_mut(unit) {
            *count -= 1;
            if *count == 0 {
                state.issuing.remove(unit);
            }
        }
        if !state.issuing.contains_key(unit) {
            let paths: Vec<_> = state
                .early
                .iter()
                .filter(|(_, e)| e.unit.as_ref() == Some(unit))
                .map(|(p, _)| p.clone())
                .collect();
            for path in paths {
                let mut event = state.early.remove(&path).unwrap();
                event.correlation = if reply_known {
                    Correlation::Unrelated
                } else {
                    Correlation::Unknown
                };
                publish.push(event);
            }
        }
        drop(state);
        for event in publish {
            self.emit(event);
        }
    }
}
pub(crate) struct RequestGuard {
    tracker: Arc<ManagerEvents>,
    unit: UnitKey,
    registered: bool,
}
impl RequestGuard {
    pub fn register(&mut self, path: &str) {
        self.tracker.finish_issuing(&self.unit, Some(path));
        self.registered = true;
    }
}
impl Drop for RequestGuard {
    fn drop(&mut self) {
        if !self.registered {
            self.tracker.finish_issuing(&self.unit, None);
        }
    }
}

// This reader has its own connection and never waits for an operation or a timer.
// Signal subscriptions are ready before callers can issue workloads.
pub(crate) async fn start_reader(
    connection: Connection,
    control: Connection,
    expected_owner: zbus::names::OwnedUniqueName,
    tracker: Arc<ManagerEvents>,
) -> Result<AbortOnDropHandle<()>> {
    let (ready, received) = oneshot::channel();
    let task_tracker = tracker.clone();
    let task = AbortOnDropHandle::new(tokio::spawn(async move {
        let result = read_events(
            connection,
            control,
            expected_owner,
            task_tracker.clone(),
            ready,
        )
        .await;
        if let Err(error) = result {
            task_tracker.fail(format!("{error:#}"));
        }
    }));
    received
        .await
        .map_err(|_| anyhow::anyhow!("event reader failed during setup"))??;
    Ok(task)
}
async fn read_events(
    connection: Connection,
    control: Connection,
    expected_owner: zbus::names::OwnedUniqueName,
    tracker: Arc<ManagerEvents>,
    ready: oneshot::Sender<Result<()>>,
) -> Result<()> {
    let manager = Proxy::new(
        &connection,
        "org.freedesktop.systemd1",
        "/org/freedesktop/systemd1",
        "org.freedesktop.systemd1.Manager",
    )
    .await?;
    // Only owner changes enter this queue, so manager event bursts cannot fill it during setup.
    let mut owner = manager.receive_owner_changed().await?;
    let control_manager = Proxy::new(
        &control,
        "org.freedesktop.systemd1",
        "/org/freedesktop/systemd1",
        "org.freedesktop.systemd1.Manager",
    )
    .await?;
    let mut control_owner = control_manager.receive_owner_changed().await?;
    let source = Proxy::new(
        &connection,
        expected_owner.as_str(),
        "/org/freedesktop/systemd1",
        "org.freedesktop.systemd1.Manager",
    )
    .await?;
    let mut signals = source.receive_all_signals().await?;
    let _ = ready.send(Ok(()));
    loop {
        tokio::select! {
            _=tracker.lost.cancelled()=>return Ok(()),
            _=owner.next()=>anyhow::bail!("manager name owner changed; previous job outcomes unknown"),
            _=control_owner.next()=>anyhow::bail!("control connection or manager owner lost; previous job outcomes unknown"),
            message=signals.next()=>{
                let message=message.ok_or_else(||anyhow::anyhow!("manager signal stream ended"))?;
                let header=message.header();let Some(member)=header.member() else{continue;};
                let scope=tracker.scope;
                match member.as_str(){
                    "JobRemoved"=>{
                        let (_,path,unit,result):(u32,zbus::zvariant::OwnedObjectPath,String,String)=message.body().deserialize()?;
                        let mut event=BackendEvent::new(scope,Some(UnitKey::new(scope,unit)),EventKind::Job,format!("systemd job {path}: {result}"));
                        event.job_path=Some(path.to_string());event.job_result=Some(result);tracker.job(event);
                    },
                    "UnitNew"|"UnitRemoved"=>{
                        let (unit,_):(String,zbus::zvariant::OwnedObjectPath)=message.body().deserialize()?;
                        let kind=if member.as_str()=="UnitNew"{EventKind::UnitNew}else{EventKind::UnitRemoved};
                        tracker.emit(BackendEvent::new(scope,Some(UnitKey::new(scope,unit)),kind,member.to_string()));
                    },
                    "Reloading"=>{
                        let active:bool=message.body().deserialize()?;
                        tracker.reloading.store(active,Ordering::SeqCst);tracker.revision.fetch_add(1,Ordering::SeqCst);
                        tracker.emit(BackendEvent::new(scope,None,if active{EventKind::Reloading}else{EventKind::Reloaded},format!("{scope} manager {}",if active{"reloading; inspection invalidated"}else{"reloaded; inspection invalidated"})));
                    },_=>{}
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn tracker() -> (Arc<ManagerEvents>, broadcast::Receiver<BackendEvent>) {
        let (tx, rx) = broadcast::channel(32);
        (
            ManagerEvents::new(Scope::User, Arc::new(AtomicU64::new(0)), tx),
            rx,
        )
    }
    fn job(unit: &UnitKey, path: &str) -> BackendEvent {
        let mut e = BackendEvent::new(
            unit.scope,
            Some(unit.clone()),
            EventKind::Job,
            "done".into(),
        );
        e.job_path = Some(path.into());
        e.job_result = Some("done".into());
        e
    }
    #[test]
    fn reply_and_completion_order_do_not_change_attribution() {
        for early in [false, true] {
            let (t, mut rx) = tracker();
            let unit = UnitKey::new(Scope::User, "a.service");
            let mut request = t.begin(unit.clone()).unwrap();
            if early {
                t.job(job(&unit, "/job/1"));
                assert!(rx.try_recv().is_err());
            }
            request.register("/job/1");
            if !early {
                t.job(job(&unit, "/job/1"));
            }
            let event = rx.try_recv().unwrap();
            assert_eq!(event.correlation, Correlation::Requested);
            assert!(t.state.lock().unwrap().early.is_empty());
            assert!(t.state.lock().unwrap().owned.is_empty());
            assert!(rx.try_recv().is_err());
        }
    }
    #[test]
    fn unrelated_jobs_during_pending_method_are_flushed_after_reply() {
        let (t, mut rx) = tracker();
        let unit = UnitKey::new(Scope::User, "a.service");
        let mut request = t.begin(unit.clone()).unwrap();
        t.job(job(&unit, "/job/other"));
        t.job(job(&unit, "/job/ours"));
        request.register("/job/ours");
        let a = rx.try_recv().unwrap();
        let b = rx.try_recv().unwrap();
        assert_eq!(a.correlation, Correlation::Requested);
        assert_eq!(b.correlation, Correlation::Unrelated);
    }
    #[tokio::test]
    async fn overflow_marks_tracking_unknown_instead_of_clearing_owned_jobs_silently() {
        let (t, _rx) = tracker();
        let unit = UnitKey::new(Scope::User, "a.service");
        let _request = t.begin(unit.clone()).unwrap();
        for i in 0..257 {
            t.job(job(&unit, &format!("/job/{i}")));
        }
        assert!(t.lost.is_cancelled());
        assert!(!t.health().available);
        assert!(t.ensure_available().is_err());
    }
}
