"""Opt-in real user manager tests; only unique devdash-py-it-* units are mutated."""

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from conftest import until

from devdash.backend import Backends
from devdash.engine import Engine
from devdash.model import Config

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("DEVDASH_SYSTEMD_TEST") != "1",
        reason="set DEVDASH_SYSTEMD_TEST=1 for dedicated user units",
    ),
]


async def test_real_user_jobs_gates_partial_restart_cancel_and_external_events(tmp_path):
    prefix = "devdash-py-it-" + uuid.uuid4().hex[:10]
    units = {key: f"{prefix}-{key}.service" for key in ("camera", "imu", "detector", "planner")}
    config = Config(
        dict(
            workloads={key: dict(backend="systemd", unit=unit) for key, unit in units.items()},
            gates={"ready": {"type": "manual"}},
            groups={
                "robot": {
                    "steps": [
                        {"parallel": ["camera", "imu"]},
                        {"gate": "ready"},
                        {"group": "perception"},
                        {"workload": "planner"},
                    ]
                },
                "perception": {"steps": [{"workload": "detector"}]},
            },
        )
    )
    backend = Backends(config)
    engine = Engine(config, backend)
    await backend.start()
    session = backend.systemd.session("user")
    runtime = Path(os.environ["XDG_RUNTIME_DIR"]) / "systemd/user"
    runtime.mkdir(parents=True, exist_ok=True)
    files = []
    try:
        for unit in units.values():
            path = runtime / unit
            path.write_text(
                '[Unit]\nDescription=devdash Python integration fixture\n[Service]\nType=simple\nExecStart=/bin/sh -c "sleep 3600 & wait"\nKillMode=control-group\nTimeoutStopSec=3\n'
            )
            files.append(path)
        await session.call("Reload")
        await engine.poll()
        assert all(obs["state"] == "inactive" for obs in engine.observations.values())
        engine.launch("robot", True, "start")
        await until(lambda: engine.run["waiting_gate"] == "ready", timeout=15)
        await engine.poll()
        assert engine.observations["camera"]["state"] == "active"
        assert engine.observations["detector"]["state"] == "inactive"
        engine.approve("ready")
        assert (await engine.wait())["status"] == "completed"
        await engine.poll()
        assert all(obs["state"] == "active" for obs in engine.observations.values())
        old = {key: obs["invocation_id"] for key, obs in engine.observations.items()}
        engine.launch("perception", True, "restart")
        assert (await engine.wait())["status"] == "completed"
        await engine.poll()
        assert engine.observations["camera"]["invocation_id"] == old["camera"]
        assert engine.observations["detector"]["invocation_id"] != old["detector"]
        engine.launch("robot", True, "restart")
        assert (await engine.wait())["status"] == "completed"
        progress = engine.run["progress"]
        assert progress[0]["step"] == "planner" and progress[0]["action"] == "stop"
        assert progress[1]["step"] == "detector" and progress[1]["action"] == "stop"
        # Another client requests a job directly, without devdash correlation.
        external = Backends(config)
        await external.start()
        try:
            await external.operate(config.workloads["planner"], "stop")
        finally:
            await external.close()
        await until(
            lambda: any(
                e["kind"] == "job"
                and e["workload"] == "planner"
                and e["correlation"] == "unrelated"
                for e in engine.events
            )
        )
        await engine.poll()
        assert engine.observations["planner"]["state"] == "inactive"
        engine.launch("robot", True, "reinitialize")
        await until(lambda: engine.run["waiting_gate"] == "ready", timeout=15)
        engine.request_cancel()
        assert (await engine.wait())["status"] == "cancelled"
        await engine.poll()
        assert engine.observations["detector"]["state"] == "inactive"
        assert any(
            e["correlation"] == "requested" and e["job_result"] == "done" for e in engine.events
        )
        # Reload must invalidate a previously published report.
        await engine.inspect("robot", "start")
        await session.call("Reload")
        await until(lambda: engine.snapshot()["inspections"]["robot"]["stale"])
    finally:
        for work in config.workloads.values():
            try:
                await asyncio.wait_for(backend.operate(work, "stop"), 10)
            except Exception:
                pass
        for path in files:
            path.unlink(missing_ok=True)
        try:
            await session.call("Reload")
        finally:
            await backend.close()


async def test_dependencies_events_and_launch_children(config, tmp_path):
    """Real metadata reload, outside operations, PID cleanup and name re-resolution."""
    import json

    from devdash.inspection import inspect
    from devdash.model import systemd_key

    prefix = "devdash-py-it-launch-" + uuid.uuid4().hex[:10]
    raw = config.json()
    for key, work in raw["workloads"].items():
        work["unit"] = f"{prefix}-{key}.service"
    config = Config(raw)
    bridge = f"{prefix}-bridge.target"
    pidfile = tmp_path / "children.json"
    script = tmp_path / "launch.py"
    script.write_text(
        "import subprocess, json, signal\nfrom pathlib import Path\n"
        'children = [subprocess.Popen(["/bin/sleep", "3600"]) for _ in range(2)]\n'
        "def stop(*args):\n"
        "    for child in children:\n"
        "        child.wait(timeout=3)\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        f"Path({str(pidfile)!r}).write_text(json.dumps([p.pid for p in children]))\n"
        "signal.pause()\n"
    )
    unit = {key: work["unit"] for key, work in config.workloads.items()}
    bodies = {}
    for key, name in unit.items():
        dependencies = (
            f"Wants={bridge}\nAfter={unit['planner']}\n"
            if key == "camera"
            else f"PartOf={unit['detector']}\n"
            if key == "planner"
            else ""
        )
        command = f'/usr/bin/python3 "{script}"' if key == "detector" else "/bin/sleep 3600"
        bodies[name] = (
            f"[Unit]\nDescription=devdash isolated launch fixture\n{dependencies}[Service]\nType=simple\nExecStart={command}\nKillMode=control-group\nTimeoutStopSec=5\n"
        )
    bodies[bridge] = (
        f"[Unit]\nDescription=devdash isolated intermediate fixture\nRequires={unit['detector']}\n"
    )
    runtime = Path(os.environ["XDG_RUNTIME_DIR"]) / "systemd/user"
    runtime.mkdir(parents=True, exist_ok=True)
    backend = Backends(config)
    external = Backends(config)
    engine = Engine(config, backend)
    await backend.start()
    await external.start()
    manager = backend.systemd.session("user")
    seeds = {systemd_key(work) for work in config.workloads.values()}
    created = []
    try:
        for name, body in bodies.items():
            path = runtime / name
            path.write_text(body)
            created.append(path)
        await manager.call("Reload")
        graph = await backend.dependencies(seeds)
        report = inspect(config, "robot", "start", set(), graph)
        assert any(
            d["code"] == "gate_bypass" and any(bridge in path for path in d["path"])
            for d in report["diagnostics"]
        ), report
        partial = inspect(config, "perception", "stop", set(), graph)
        assert any(
            d["code"] == "outside_stop" and {"scope": "user", "unit": unit["planner"]} in d["units"]
            for d in partial["diagnostics"]
        ), partial
        (runtime / unit["camera"]).write_text(
            f"[Unit]\nAfter={unit['planner']}\n[Service]\nExecStart=/bin/sleep 3600\n"
        )
        await manager.call("Reload")
        fresh = inspect(config, "robot", "start", set(), await backend.dependencies(seeds))
        assert not any(d["code"] == "gate_bypass" for d in fresh["diagnostics"])
        assert any(d["code"] == "ordering" for d in fresh["diagnostics"])
        await engine.poll()
        await external.operate(config.workloads["detector"], "start")
        await until(lambda: pidfile.exists(), timeout=5)
        await engine.poll()
        assert engine.observations["detector"]["state"] == "active"
        assert engine.run["status"] == ""  # Observation never initiates repair.
        assert any(
            e["kind"] == "gate_mismatch" and e["workload"] == "detector" for e in engine.events
        )
        assert any(
            e["kind"] == "job" and e["correlation"] == "unrelated" and e["workload"] == "detector"
            for e in engine.events
        )
        pids = json.loads(pidfile.read_text())
        assert len(pids) == 2 and all(Path(f"/proc/{pid}").exists() for pid in pids)
        await external.operate(config.workloads["detector"], "stop")
        await until(lambda: all(not Path(f"/proc/{pid}").exists() for pid in pids), timeout=5)
        await engine.poll()
        assert engine.observations["detector"]["state"] == "inactive"
        (runtime / unit["detector"]).unlink()
        await manager.call("Reload")
        await engine.poll()
        assert engine.observations["detector"]["state"] == "not_found"
        assert "detector" in engine.config.workloads
        (runtime / unit["detector"]).write_text(bodies[unit["detector"]])
        await manager.call("Reload")
        await engine.poll()
        assert engine.observations["detector"]["state"] == "inactive"
        assert "detector" in engine.config.workloads
    finally:
        # Restore removed definitions so even a failing test can stop its own units.
        for path in created:
            path.write_text(bodies[path.name])
        await manager.call("Reload")
        errors = []
        for path in created:
            try:
                await asyncio.wait_for(
                    backend.systemd.operate({"scope": "user", "unit": path.name}, "stop"), 10
                )
            except Exception as error:
                errors.append(f"{path.name}: {error}")
            path.unlink(missing_ok=True)
        await manager.call("Reload")
        await external.close()
        await backend.close()
        assert not errors, errors


async def test_both_managers_readonly():
    config = Config({})
    backend = Backends(config)
    await backend.start()
    try:
        user = set(await backend.scan("user"))
        system = set(await backend.scan("system"))
        assert user and system
        common = user & system
        if not common:
            pytest.skip("No same-named service available on this host for read-only comparison")
        name = "dbus.service" if "dbus.service" in common else sorted(common)[0]
        user_id = config.import_unit(name, "user")
        system_id = config.import_unit(name, "system")
        assert user_id != system_id
        for key in (user_id, system_id):
            result = await backend.observe(config.workloads[key])
            assert result["state"] != "not_found", (key, result)
        assert backend.systemd.session("user").bus is not backend.systemd.session("system").bus
    finally:
        await backend.close()


async def test_resident_core_user_unit_restart_does_not_touch_workload(tmp_path):
    """Run the supplied template under a unique name; never enable or set linger."""
    import signal
    import sys

    import yaml

    from devdash.installation import template
    from devdash.ipc import request

    prefix = "devdash-py-it-resident-" + uuid.uuid4().hex[:8]
    core_name, worker_name = prefix + "-core.service", prefix + "-worker.service"
    runtime_name = prefix + "-core"
    runtime = Path(os.environ["XDG_RUNTIME_DIR"])
    socket = runtime / runtime_name / "core.sock"
    config_path, state_path = tmp_path / "config.yaml", tmp_path / "approvals.json"
    config = Config(
        {
            "workloads": {"worker": {"backend": "systemd", "unit": worker_name}},
            "groups": {"robot": {"steps": [{"workload": "worker"}, {"gate": "ready"}]}},
            "gates": {"ready": {"type": "manual"}},
        }
    )
    config_path.write_text(yaml.safe_dump(config.json()))
    body = template("core")
    lines = body.splitlines()
    lines = [
        f'ExecStart="{sys.executable}" -I -m devdash --socket "%t/{runtime_name}/core.sock" daemon --config "{config_path}" --state "{state_path}"'
        if line.startswith("ExecStart=")
        else f"RuntimeDirectory={runtime_name}"
        if line.startswith("RuntimeDirectory=")
        else line
        for line in lines
    ]
    bodies = {
        core_name: "\n".join(lines) + "\n",
        worker_name: "[Unit]\nDescription=devdash isolated resident workload\n[Service]\nExecStart=/bin/sleep 3600\nKillMode=control-group\n",
    }
    directory = runtime / "systemd/user"
    directory.mkdir(parents=True, exist_ok=True)
    backend = Backends(config)
    await backend.start()
    manager = backend.systemd.session("user")
    worker = config.workloads["worker"]
    files = []

    async def control_core(action):
        # Core lifecycle belongs to the operator/systemd, never to Workload APIs.
        process = await asyncio.create_subprocess_exec(
            "systemctl",
            "--user",
            action,
            core_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, error = await asyncio.wait_for(process.communicate(), 15)
        assert process.returncode == 0, error.decode()

    async def core_when(predicate):
        async def poll():
            while True:
                try:
                    value = await request(socket, {"command": "snapshot"})
                    if predicate(value):
                        return value
                except OSError:
                    pass
                await asyncio.sleep(0.02)

        return await asyncio.wait_for(poll(), 15)

    async def unchanged(original):
        current = await backend.observe(worker)
        assert current["state"] == "active"
        assert (current["pid"], current["invocation_id"]) == (
            original["pid"],
            original["invocation_id"],
        )

    try:
        for name, value in bodies.items():
            path = directory / name
            path.write_text(value)
            files.append(path)
        await manager.call("Reload")
        await backend.operate(worker, "start")
        original = await backend.observe(worker)
        await control_core("start")
        initial = await core_when(
            lambda value: value["observations"].get("worker", {}).get("state") == "active"
        )
        assert initial["run"]["status"] == "" and not initial["approved"]
        assert core_name not in await request(socket, {"command": "scan", "scope": "user"})
        before_import = config_path.read_text()
        with pytest.raises(RuntimeError, match="not available|cannot manage itself"):
            await request(socket, {"command": "import", "scope": "user", "units": [core_name]})
        assert config_path.read_text() == before_import
        assert socket.stat().st_mode & 0o777 == 0o600
        await unchanged(original)
        await request(socket, dict(command="operate", target="robot", group=True, action="start"))
        await core_when(lambda value: value["run"]["waiting_gate"] == "ready")
        await manager.call("KillUnit", "ssi", [core_name, "main", signal.SIGKILL])
        # Restart=on-failure performs this restart, not devdash.
        recovered = await core_when(
            lambda value: (
                value["core"]["instance"] != initial["core"]["instance"]
                and value["observations"].get("worker", {}).get("state") == "active"
            )
        )
        assert recovered["run"]["status"] == "" and not recovered["approved"]
        await unchanged(original)
        await control_core("stop")
        assert not socket.exists()
        await unchanged(original)
        await control_core("start")
        restarted = await core_when(
            lambda value: value["observations"].get("worker", {}).get("state") == "active"
        )
        assert restarted["core"]["instance"] != recovered["core"]["instance"]
        assert restarted["run"]["status"] == ""
        await unchanged(original)
        await request(socket, dict(command="operate", target="robot", group=True, action="start"))
        await core_when(lambda value: value["run"]["waiting_gate"] == "ready")
        await request(socket, {"command": "approve", "gate": "ready"})
        await core_when(lambda value: value["run"]["status"] == "completed")
        await control_core("stop")
        await control_core("start")
        restored = await core_when(
            lambda value: value["observations"].get("worker", {}).get("state") == "active"
        )
        assert restored["approved"] == ["ready"] and restored["run"]["status"] == ""
        await unchanged(original)
        assert state_path.stat().st_mode & 0o777 == 0o600
    finally:
        errors = []
        for name in (core_name, worker_name):
            if any(path.name == name for path in files):
                try:
                    if name == core_name:
                        await control_core("stop")
                    else:
                        await asyncio.wait_for(
                            backend.systemd.operate({"scope": "user", "unit": name}, "stop"), 10
                        )
                except Exception as error:
                    errors.append(str(error))
                try:
                    await manager.call("ResetFailedUnit", "s", [name])
                except Exception:
                    pass
        for path in files:
            path.unlink(missing_ok=True)
        await manager.call("Reload")
        await backend.close()
        assert not errors, errors
        private_runtime = runtime / runtime_name
        (private_runtime / "core.sock").unlink(missing_ok=True)
        (private_runtime / "core.lock").unlink(missing_ok=True)
        if private_runtime.exists():
            private_runtime.rmdir()
