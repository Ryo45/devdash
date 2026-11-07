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
