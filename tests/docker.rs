//! Explicitly opted-in Docker integration; only unique devdash-it-* containers.
use anyhow::{Context, Result, ensure};
use devdash::{
    children::{ChildAction, ChildKind},
    engine::{Action, Backend, Engine, Observation},
    model::*,
    runtime::Backends,
};
use serde_json::json;
use std::{
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
async fn docker(args: &[&str]) -> Result<String> {
    let output = tokio::process::Command::new("docker")
        .args(args)
        .output()
        .await?;
    ensure!(
        output.status.success(),
        "docker {args:?}: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    Ok(String::from_utf8(output.stdout)?.trim().into())
}
async fn observe(
    backend: &dyn Backend,
    w: &Workload,
    predicate: impl Fn(&Observation) -> bool,
) -> Result<Observation> {
    tokio::time::timeout(Duration::from_secs(25), async {
        loop {
            let o = backend.observe(w).await?;
            if predicate(&o) {
                return Ok(o);
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    })
    .await?
}
async fn complete(e: &Engine) -> Result<()> {
    let run = tokio::time::timeout(Duration::from_secs(30), e.wait()).await?;
    ensure!(run.status == "completed", "{run:?}");
    Ok(())
}
#[tokio::test]
#[ignore = "creates dedicated test container; build tests/fixtures/supervisor/Dockerfile first"]
async fn supervisor_services_health_epochs_and_container_recreation() -> Result<()> {
    let name = format!(
        "devdash-it-{}-{}",
        std::process::id(),
        SystemTime::now().duration_since(UNIX_EPOCH)?.as_millis()
    );
    let image =
        std::env::var("DEVDASH_TEST_IMAGE").unwrap_or("devdash-supervisor-test:4.3.0".into());
    let project = format!("com.docker.compose.project={name}");
    let create = || {
        vec![
            "create",
            "--name",
            name.as_str(),
            "--network",
            "none",
            "--cpus",
            "1",
            "--memory",
            "512m",
            "--label",
            project.as_str(),
            "--label",
            "com.docker.compose.service=robot",
            image.as_str(),
        ]
    };
    docker(&create()).await?;
    let result=async {
        let config=Config::parse(&json!({"workloads":{"robot":{"backend":"docker","docker":{"compose":{"project":name,"service":"robot"}},"controller":{"kind":"supervisord","socket":"/tmp/devdash-supervisor.sock"}}},"groups":{"robot":{"steps":[{"workload":"robot"},{"gate":"ready"},{"gate":"operator"}]}},"gates":{"ready":{"type":"container_ready","workload":"robot","timeout_ms":20000,"interval_ms":100},"operator":{"type":"manual"}}}).to_string())?;
        let w=&config.workloads["robot"];
        let backend=Arc::new(Backends::connect().await?);
        let e=Engine::new(config.clone(),backend.clone(),None,"test".into())?;
        e.launch("robot".into(),true,Action::Start)?;
        tokio::time::timeout(Duration::from_secs(25),async{while e.snapshot().waiting_gate.as_deref()!=Some("operator"){ensure!(e.snapshot().status!="failed","{:?}",e.snapshot());tokio::time::sleep(Duration::from_millis(50)).await;}Ok::<_,anyhow::Error>(())}).await??;
        let initial=observe(&*backend,w,|o|o.children.iter().any(|c|c.id=="worker:worker"&&c.state=="RUNNING")).await?;
        ensure!(initial.health.as_deref()==Some("healthy"));
        let worker=initial.children.iter().find(|c|c.id=="worker:worker").context("worker missing")?.clone();
        let idle=initial.children.iter().find(|c|c.id=="idle:idle").context("stopped service missing")?.clone();
        ensure!(idle.pid.is_none()&&idle.actions.contains(&ChildAction::Start));
        ensure!(e.launch_child("robot".into(),idle.id.clone(),idle.epoch.clone(),Action::Start).is_err(),"child/group conflict must be rejected");
        e.approve("operator")?;complete(&e).await?;
        let raw=initial.children.iter().find(|c|c.kind==ChildKind::Process).context("Docker top did not return OS processes")?;
        ensure!(raw.actions.is_empty());
        e.launch_child("robot".into(),raw.id.clone(),raw.epoch.clone(),Action::Stop)?;
        ensure!(e.wait().await.status=="failed","raw PID must never become a lifecycle target");
        e.launch_child("robot".into(),worker.id.clone(),worker.epoch.clone(),Action::Stop)?;complete(&e).await?;
        let stopped=observe(&*backend,w,|o|o.children.iter().any(|c|c.id==worker.id&&c.state=="STOPPED")).await?;
        ensure!(stopped.state=="active"&&stopped.health.as_deref()==Some("healthy"),"container-defined health must not be redefined from children");
        let known=stopped.children.iter().find(|c|c.id==worker.id).unwrap();ensure!(known.epoch==worker.epoch&&known.pid.is_none());
        e.launch_child("robot".into(),idle.id.clone(),idle.epoch.clone(),Action::Start)?;complete(&e).await?;
        let running=observe(&*backend,w,|o|o.children.iter().any(|c|c.id==idle.id&&c.pid.is_some())).await?;
        let pid=running.children.iter().find(|c|c.id==idle.id).unwrap().pid;
        e.launch_child("robot".into(),idle.id.clone(),idle.epoch.clone(),Action::Restart)?;complete(&e).await?;
        observe(&*backend,w,|o|o.children.iter().any(|c|c.id==idle.id&&c.pid.is_some()&&c.pid!=pid)).await?;
        ensure!(backend.logs(w).await?.contains("devdash isolated supervisor worker ready"));
        e.launch("robot".into(),false,Action::Restart)?;complete(&e).await?;
        let restarted=observe(&*backend,w,|o|o.children.iter().any(|c|c.id==worker.id&&c.state=="RUNNING")).await?;
        ensure!(restarted.children.iter().find(|c|c.id==worker.id).unwrap().epoch!=worker.epoch,"container restart must change child epoch even if supervisord is PID 1 again");
        e.launch_child("robot".into(),worker.id.clone(),worker.epoch.clone(),Action::Stop)?;ensure!(e.wait().await.status=="failed","stale epoch must be rejected");
        docker(&["rm","-f",&name]).await?;
        ensure!(backend.observe(w).await?.state=="not_found");
        docker(&create()).await?;
        e.launch("robot".into(),false,Action::Start)?;complete(&e).await?;
        let recreated=observe(&*backend,w,|o|o.children.iter().any(|c|c.id==worker.id&&c.state=="RUNNING")).await?;
        ensure!(recreated.canonical_unit!=initial.canonical_unit,"Compose selector must re-resolve recreation");
        ensure!(recreated.children.iter().find(|c|c.id==idle.id).unwrap().state=="STOPPED","devdash must not reconcile previously started children after container recreation");
        // A second configuration without a controller exposes only observations.
        let mut readonly=w.clone();readonly.controller=None;
        let observed=backend.observe(&readonly).await?;ensure!(observed.children.iter().all(|c|c.actions.is_empty()));
        ensure!(backend.operate_child(&readonly,&worker.id,&worker.epoch,Action::Start).await.is_err());
        eprintln!("Verified supervisor stop/start/restart, stopped services, health separation, conflict rejection, stale epochs, raw PID refusal and Compose recreation: {name}");
        Ok::<_,anyhow::Error>(())
    }.await;
    let cleanup = docker(&["rm", "-f", &name]).await;
    result?;
    cleanup?;
    Ok(())
}
