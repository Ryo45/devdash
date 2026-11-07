//! Explicitly opted-in integration test; touches only unique devdash-it-* runtime units.
use anyhow::{Result, ensure};
use devdash::{
    engine::{Action, Backend, Engine},
    model::*,
    systemd::Systemd,
};
use std::{
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use zbus::{Proxy, zvariant::OwnedObjectPath};
// These tests share one manager and issue daemon-reload; keep their fixtures isolated.
static MANAGER_TEST: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());
async fn waiting(e: &Engine) -> Result<()> {
    tokio::time::timeout(Duration::from_secs(10), async {
        loop {
            if e.snapshot().waiting_gate.as_deref() == Some("sensors_ready") {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await?;
    Ok(())
}
async fn finished(e: &Engine) -> Result<()> {
    let r = tokio::time::timeout(Duration::from_secs(15), e.wait()).await?;
    ensure!(r.status == "completed", "{r:?}");
    Ok(())
}
#[tokio::test]
#[ignore = "creates dedicated runtime systemd user units; run explicitly in a user session"]
async fn robot_real_systemd() -> Result<()> {
    let _manager_test = MANAGER_TEST.lock().await;
    let conn = zbus::Connection::session().await?;
    let manager = Proxy::new(
        &conn,
        "org.freedesktop.systemd1",
        "/org/freedesktop/systemd1",
        "org.freedesktop.systemd1.Manager",
    )
    .await?;
    let prefix = format!(
        "devdash-it-{}-{}",
        std::process::id(),
        SystemTime::now().duration_since(UNIX_EPOCH)?.as_millis()
    );
    let dir = tempfile::tempdir()?;
    let mut cfg = Config::parse(include_str!("../examples/robot.yaml"))?;
    cfg.workloads.insert(
        "failure".into(),
        Workload {
            name: String::new(),
            backend: BackendKind::Systemd,
            docker: None,
            controller: None,
            scope: Scope::User,
            unit: String::new(),
        },
    );
    let mut files = Vec::new();
    let mut names = Vec::new();
    for (id, w) in &mut cfg.workloads {
        w.unit = format!("{prefix}-{id}.service");
        names.push(w.unit.clone());
        let path = dir.path().join(&w.unit);
        let body = if id == "failure" {
            "[Unit]\nDescription=devdash isolated failure test\n[Service]\nType=oneshot\nExecStart=/usr/bin/false\n"
        } else {
            "[Unit]\nDescription=devdash isolated integration dummy\n[Service]\nType=simple\nExecStart=/usr/bin/sleep infinity\n"
        };
        let body = if id == "camera" {
            format!("{body}\n[Install]\nAlias={prefix}-camera-alias.service\n")
        } else {
            body.to_string()
        };
        std::fs::write(&path, body)?;
        files.push(path.to_string_lossy().to_string());
    }
    let _: Vec<(String, String, String)> = manager
        .call("LinkUnitFiles", &(&files, true, false))
        .await?;
    let _: () = manager.call("Reload", &()).await?;
    let backend = Arc::new(Systemd::connect().await?);
    let result = async {
        // Enable only the fixture's runtime alias; no boot enablement or target links.
        let _reply = manager
            .call_method(
                "EnableUnitFiles",
                &(&vec![cfg.workloads["camera"].unit.clone()], true, false),
            )
            .await?;
        let _: () = manager.call("Reload", &()).await?;
        cfg.workloads.get_mut("camera").unwrap().unit = format!("{prefix}-camera-alias.service");
        let e = Engine::new(cfg.clone(), backend.clone(), None, "test-boot".into())?;
        e.launch("robot".into(), true, Action::Start)?;
        waiting(&e).await?;
        ensure!(backend.observe(&cfg.workloads["camera"]).await?.state == "active");
        ensure!(backend.observe(&cfg.workloads["detector"]).await?.state == "inactive");
        e.approve("sensors_ready")?;
        finished(&e).await?;
        let camera_pid = backend.observe(&cfg.workloads["camera"]).await?.pid;
        ensure!(camera_pid.is_some());
        e.launch("perception".into(), true, Action::Restart)?;
        finished(&e).await?;
        ensure!(
            backend.observe(&cfg.workloads["camera"]).await?.pid == camera_pid,
            "partial restart touched sensors"
        );
        e.launch("robot".into(), true, Action::Restart)?;
        finished(&e).await?;
        let run = e.snapshot();
        let completed: Vec<_> = run
            .progress
            .iter()
            .filter(|p| p.status == "completed" && cfg.workloads.contains_key(&p.step))
            .collect();
        ensure!(
            completed
                .first()
                .is_some_and(|p| p.step == "planner" && p.action == Action::Stop)
        );
        ensure!(
            completed
                .last()
                .is_some_and(|p| p.step == "planner" && p.action == Action::Start)
        );
        e.launch("robot".into(), true, Action::Reinitialize)?;
        waiting(&e).await?;
        e.cancel()?;
        let run = tokio::time::timeout(Duration::from_secs(10), e.wait()).await?;
        ensure!(run.status == "cancelled");
        ensure!(backend.observe(&cfg.workloads["detector"]).await?.state == "inactive");
        ensure!(backend.observe(&cfg.workloads["camera"]).await?.state == "active");
        let scanned = backend.scan(Scope::User).await?;
        ensure!(names.iter().all(|n| scanned.contains(n)));
        e.launch("failure".into(), false, Action::Start)?;
        let failure = tokio::time::timeout(Duration::from_secs(10), e.wait()).await?;
        ensure!(failure.status == "failed");
        ensure!(
            failure.progress.iter().any(|p| p
                .error
                .as_ref()
                .is_some_and(|e| e.contains("job result failed"))),
            "must report the actual failed job result: {failure:?}"
        );
        let missing = Workload {
            name: String::new(),
            backend: BackendKind::Systemd,
            docker: None,
            controller: None,
            scope: Scope::User,
            unit: format!("{prefix}-missing.service"),
        };
        ensure!(backend.observe(&missing).await?.state == "not_found");
        eprintln!(
            "Verified gate, approval, partial restart, full restart, cancel and scan on {prefix}"
        );
        Ok::<_, anyhow::Error>(())
    }
    .await;
    // Always attempt cleanup, even if a scenario failed. These are ONLY our unique units.
    let mut cleanup_errors = Vec::new();
    for w in cfg.workloads.values() {
        if let Err(e) = backend.operate(w, Action::Stop).await {
            cleanup_errors.push(e.to_string());
        }
    }
    for name in &names {
        let _: Result<(), _> = manager.call("ResetFailedUnit", &(name.as_str(),)).await;
    }
    let unlink: Result<Vec<(String, String, String)>, _> =
        manager.call("DisableUnitFiles", &(&names, true)).await;
    if let Err(e) = unlink {
        cleanup_errors.push(e.to_string());
    }
    let reload: Result<(), _> = manager.call("Reload", &()).await;
    if let Err(e) = reload {
        cleanup_errors.push(e.to_string());
    }
    ensure!(
        cleanup_errors.is_empty(),
        "cleanup errors: {cleanup_errors:?}; original scenario: {result:?}"
    );
    result
}

#[tokio::test]
#[ignore = "dedicated runtime user units and child processes; requires a user session"]
async fn dependencies_events_and_launch_children() -> Result<()> {
    let _manager_test = MANAGER_TEST.lock().await;
    use devdash::{
        inspection,
        ipc::{self, Request, Snapshot},
    };
    use std::collections::BTreeSet;
    let conn = zbus::Connection::session().await?;
    let manager = Proxy::new(
        &conn,
        "org.freedesktop.systemd1",
        "/org/freedesktop/systemd1",
        "org.freedesktop.systemd1.Manager",
    )
    .await?;
    let prefix = format!(
        "devdash-it-launch-{}-{}",
        std::process::id(),
        SystemTime::now().duration_since(UNIX_EPOCH)?.as_millis()
    );
    let dir = tempfile::tempdir()?;
    let mut cfg = Config::parse(include_str!("../examples/robot.yaml"))?;
    for (id, w) in &mut cfg.workloads {
        w.unit = format!("{prefix}-{id}.service");
    }
    let bridge = format!("{prefix}-bridge.target");
    let pidfile = dir.path().join("children.json");
    let launch = dir.path().join("launch.py");
    std::fs::write(
        &launch,
        format!(
            "import subprocess, json, time\nfrom pathlib import Path\nchildren=[subprocess.Popen(['/usr/bin/sleep','infinity']) for _ in range(2)]\nPath({:?}).write_text(json.dumps([c.pid for c in children]))\nwhile True: time.sleep(1)\n",
            pidfile.to_string_lossy()
        ),
    )?;
    let mut bodies = std::collections::BTreeMap::new();
    for (id, w) in &cfg.workloads {
        let dependencies = match id.as_str() {
            "camera" => format!("Wants={bridge}\nAfter={}\n", cfg.workloads["planner"].unit),
            "planner" => format!("PartOf={}\n", cfg.workloads["detector"].unit),
            _ => String::new(),
        };
        let exec = if id == "detector" {
            format!("/usr/bin/python3 {}", launch.display())
        } else {
            "/usr/bin/sleep infinity".into()
        };
        bodies.insert(w.unit.clone(),format!("[Unit]\nDescription=devdash isolated launch test\n{dependencies}[Service]\nType=simple\nKillMode=control-group\nExecStart={exec}\n"));
    }
    bodies.insert(
        bridge.clone(),
        format!(
            "[Unit]\nDescription=devdash isolated intermediate target\nRequires={}\n",
            cfg.workloads["detector"].unit
        ),
    );
    let names: Vec<_> = bodies.keys().cloned().collect();
    let mut files = Vec::new();
    for (name, body) in &bodies {
        let path = dir.path().join(name);
        std::fs::write(&path, body)?;
        files.push(path.to_string_lossy().into_owned());
    }
    let _: Vec<(String, String, String)> = manager
        .call("LinkUnitFiles", &(&files, true, false))
        .await?;
    let _: () = manager.call("Reload", &()).await?;
    eprintln!("{prefix}: connecting backend");
    let backend =
        Arc::new(tokio::time::timeout(Duration::from_secs(12), Systemd::connect()).await??);
    let result=tokio::time::timeout(Duration::from_secs(30),async {
        eprintln!("{prefix}: inspecting dependencies");
        let seeds:Vec<_>=cfg.workloads.values().map(UnitKey::from).collect();
        let graph=backend.dependencies(&seeds).await?;
        let report=inspection::inspect(&cfg,"robot",Action::Start,&BTreeSet::new(),&graph)?;
        ensure!(report.diagnostics.iter().any(|d|d.code=="gate_bypass"&&d.path.iter().any(|p|p.contains(&bridge))),"missing indirect bypass: {report:?}");
        ensure!(inspection::inspect(&cfg,"perception",Action::Stop,&BTreeSet::new(),&graph)?.diagnostics.iter().any(|d|d.code=="outside_stop"&&d.units.iter().any(|u|u.unit==cfg.workloads["planner"].unit)));
        // Re-read actual D-Bus metadata after daemon-reload, preserving only After.
        let camera=&cfg.workloads["camera"].unit;
        std::fs::write(dir.path().join(camera),format!("[Unit]\nDescription=devdash isolated ordering-only test\nAfter={}\n[Service]\nExecStart=/usr/bin/sleep infinity\n",cfg.workloads["planner"].unit))?;
        let _:()=manager.call("Reload",&()).await?;
        let graph=backend.dependencies(&seeds).await?;let report=inspection::inspect(&cfg,"robot",Action::Start,&BTreeSet::new(),&graph)?;
        ensure!(!report.diagnostics.iter().any(|d|d.code=="gate_bypass"),"stale dependency cache: {report:?}");ensure!(report.diagnostics.iter().any(|d|d.code=="order_mismatch"));
        eprintln!("{prefix}: dependency reload verified; starting observer");
        // Core receives outside operations; it must only observe, never repair them.
        let config_path=dir.path().join("config.yaml");std::fs::write(&config_path,serde_yaml::to_string(&cfg)?)?;
        let e=Engine::new(cfg.clone(),backend.clone(),None,"boot".into())?;let socket=dir.path().join("core.sock");
        let server={let e=e.clone();let socket=socket.clone();tokio::spawn(async move{ipc::serve(e,&socket,config_path).await})};
        let server=tokio_util::task::AbortOnDropHandle::new(server);
        tokio::time::timeout(Duration::from_secs(3),async{while ipc::request(&socket,Request::Snapshot).await.is_err(){tokio::time::sleep(Duration::from_millis(20)).await;}}).await?;
        let _:OwnedObjectPath=manager.call("StartUnit",&(cfg.workloads["detector"].unit.as_str(),"replace")).await?;
        let snapshot=tokio::time::timeout(Duration::from_secs(12),async {loop {
            let s:Snapshot=serde_json::from_value(ipc::request(&socket,Request::Snapshot).await?.data)?;
            if s.events.iter().any(|e|e.workload.as_deref()==Some("detector")&&e.message.contains("not correlated"))&&s.events.iter().any(|e|e.message.contains("gate sensors_ready")){break Ok::<_,anyhow::Error>(s);}
            tokio::time::sleep(Duration::from_millis(100)).await;
        }}).await??;
        ensure!(snapshot.run.status.is_empty(),"observation must not launch a sequence");
        ensure!(backend.observe(&cfg.workloads["detector"]).await?.state=="active","no automatic repair/stop");
        tokio::time::timeout(Duration::from_secs(3),async{while !pidfile.exists(){tokio::time::sleep(Duration::from_millis(20)).await;}}).await?;
        let pids:Vec<u32>=serde_json::from_slice(&std::fs::read(&pidfile)?)?;ensure!(pids.len()==2);ensure!(pids.iter().all(|p|std::path::Path::new(&format!("/proc/{p}")).exists()));
        let _:OwnedObjectPath=manager.call("StopUnit",&(cfg.workloads["detector"].unit.as_str(),"replace")).await?;
        tokio::time::timeout(Duration::from_secs(8),async{loop{if backend.observe(&cfg.workloads["detector"]).await?.state=="inactive"&&pids.iter().all(|p|!std::path::Path::new(&format!("/proc/{p}")).exists()){break Ok::<_,anyhow::Error>(());}tokio::time::sleep(Duration::from_millis(50)).await;}}).await??;
        // Missing and restored definitions remain registered and are resolved by name.
        let detector=&cfg.workloads["detector"].unit;std::fs::remove_file(dir.path().join(detector))?;let _:()=manager.call("Reload",&()).await?;
        tokio::time::timeout(Duration::from_secs(8),async{loop{let s:Snapshot=serde_json::from_value(ipc::request(&socket,Request::Snapshot).await?.data)?;if s.observations["detector"].state=="not_found"{break Ok::<_,anyhow::Error>(());}tokio::time::sleep(Duration::from_millis(100)).await;}}).await??;
        std::fs::write(dir.path().join(detector),&bodies[detector])?;let _:()=manager.call("Reload",&()).await?;
        tokio::time::timeout(Duration::from_secs(8),async{loop{let s:Snapshot=serde_json::from_value(ipc::request(&socket,Request::Snapshot).await?.data)?;if s.observations["detector"].state=="inactive"{ensure!(s.config.workloads.contains_key("detector"));break Ok::<_,anyhow::Error>(());}tokio::time::sleep(Duration::from_millis(100)).await;}}).await??;
        server.abort();
        eprintln!("Verified actual dependencies/reload, outside jobs, gate mismatch, missing/re-added unit and all launch children stopped: {prefix}");
        Ok::<_,anyhow::Error>(())
    }).await.unwrap_or_else(|e|Err(anyhow::anyhow!("launch scenario timed out: {e}")));
    eprintln!("{prefix}: cleaning fixtures");
    // Restore definitions before cleanup even when the deletion test failed halfway.
    for (name, body) in &bodies {
        std::fs::write(dir.path().join(name), body)?;
    }
    let _: () = manager.call("Reload", &()).await?;
    let mut errors = Vec::new();
    for w in cfg.workloads.values() {
        if let Err(e) = backend.operate(w, Action::Stop).await {
            errors.push(e.to_string());
        }
    }
    let _: Result<OwnedObjectPath, _> = manager
        .call("StopUnit", &(bridge.as_str(), "replace"))
        .await;
    for name in &names {
        let _: Result<(), _> = manager.call("ResetFailedUnit", &(name.as_str(),)).await;
    }
    let _: Vec<(String, String, String)> =
        manager.call("DisableUnitFiles", &(&names, true)).await?;
    let _: () = manager.call("Reload", &()).await?;
    ensure!(
        errors.is_empty(),
        "cleanup: {errors:?}; scenario: {result:?}"
    );
    result
}

#[tokio::test]
#[ignore = "read-only scans/observations of actual user and system managers"]
async fn both_managers_readonly() -> Result<()> {
    let _manager_test = MANAGER_TEST.lock().await;
    let backend = Systemd::connect().await?;
    let user = tokio::time::timeout(Duration::from_secs(10), backend.scan(Scope::User)).await??;
    let system =
        tokio::time::timeout(Duration::from_secs(10), backend.scan(Scope::System)).await??;
    ensure!(!user.is_empty() && !system.is_empty());
    let same = user
        .iter()
        .find(|unit| system.contains(unit))
        .ok_or_else(|| {
            anyhow::anyhow!("no same-named unit available for this host's read-only test")
        })?;
    let mut config = Config {
        workloads: Default::default(),
        groups: Default::default(),
        gates: Default::default(),
    };
    let user_id = config.import(same, Scope::User)?;
    let system_id = config.import(same, Scope::System)?;
    ensure!(user_id != system_id);
    for id in [&user_id, &system_id] {
        let observation = tokio::time::timeout(
            Duration::from_secs(10),
            backend.observe(&config.workloads[id]),
        )
        .await??;
        ensure!(observation.state != "not_found");
    }
    eprintln!(
        "Read-only manager validation: {} user / {} system services; {same} resolves independently",
        user.len(),
        system.len()
    );
    Ok(())
}
