import asyncio
import copy

import pytest
from aiohttp import web
from conftest import until

from devdash.engine import Engine
from devdash.model import Config, ConfigError, UnitKey, observation


async def test_forward_reverse_and_partial_restart(engine, backend):
    engine.approve("ready")
    engine.launch("robot", True, "start")
    assert (await engine.wait())["status"] == "completed"
    assert set(backend.calls[:2]) == {("camera", "start"), ("imu", "start")}
    assert backend.calls[2:] == [("detector", "start"), ("planner", "start")]
    backend.calls.clear()
    engine.launch("perception", True, "restart")
    await engine.wait()
    assert backend.calls == [("detector", "stop"), ("detector", "start")]
    backend.calls.clear()
    engine.launch("robot", True, "restart")
    assert (await engine.wait())["status"] == "completed"
    assert backend.calls[:2] == [("planner", "stop"), ("detector", "stop")]
    assert set(backend.calls[2:4]) == {("camera", "stop"), ("imu", "stop")}
    assert backend.calls[-2:] == [("detector", "start"), ("planner", "start")]


async def test_manual_gate_individual_bypass_and_reinitialize(engine, backend):
    engine.launch("detector", False, "start")
    await engine.wait()
    assert backend.calls == [("detector", "start")]
    backend.calls.clear()
    engine.launch("robot", True, "start")
    await until(lambda: engine.run["waiting_gate"] == "ready")
    assert len(backend.calls) == 2
    engine.approve("ready")
    assert (await engine.wait())["status"] == "completed"
    engine.launch("robot", True, "reinitialize")
    await until(lambda: engine.run["waiting_gate"] == "ready")
    assert "ready" not in engine.approved
    engine.request_cancel()
    assert (await engine.wait())["status"] == "cancelled"
    assert engine.run["progress"][-1]["status"] == "skipped"


async def test_cancel_drains_parallel_and_rejects_conflict(engine, backend):
    backend.block = {key: asyncio.Event() for key in ("camera", "imu")}
    engine.launch("robot", True, "restart")
    await until(lambda: len(backend.calls) == 4)
    with pytest.raises(ValueError, match="Another"):
        engine.launch("planner", False, "restart")
    engine.request_cancel()
    assert engine.run["status"] == "cancelling"
    assert not engine.task.done()
    for event in backend.block.values():
        event.set()
    result = await engine.wait()
    assert result["status"] == "cancelled"
    assert all(action == "stop" for _, action in backend.calls)
    assert all(
        step["status"] == "skipped" for step in result["progress"] if step["action"] == "start"
    )


async def test_parallel_failure_drains_and_no_rollback(engine, backend):
    backend.fail.add("camera")
    backend.block["imu"] = asyncio.Event()
    engine.launch("robot", True, "start")
    await until(lambda: len(backend.calls) == 2)
    assert not engine.task.done()
    backend.block["imu"].set()
    result = await engine.wait()
    assert result["status"] == "failed"
    assert len(backend.calls) == 2
    assert all(action == "start" for _, action in backend.calls)
    assert result["progress"][-1]["status"] == "skipped"


async def test_lost_manager_during_gate_never_resumes(engine, backend):
    engine.launch("robot", True, "start")
    await until(lambda: engine.run["waiting_gate"] == "ready")
    backend.lost.set()
    assert (await engine.wait())["status"] == "failed"
    engine.approve("ready")
    await asyncio.sleep(0.02)
    assert len(backend.calls) == 2


def test_boot_persistence(config, backend, tmp_path):
    path = tmp_path / "approvals.json"
    first = Engine(config, backend, path, "boot-a")
    first.approve("ready")
    assert Engine(config, backend, path, "boot-a").approved == {"ready"}
    assert Engine(config, backend, path, "boot-b").approved == set()


@pytest.mark.parametrize(
    "mutation",
    ["cycle", "duplicate_workload", "duplicate_group", "unknown", "badstep", "badbackend"],
)
def test_validation(config, mutation):
    raw = config.json()
    if mutation == "cycle":
        raw["groups"]["sensors"]["steps"].append({"group": "robot"})
    elif mutation == "duplicate_workload":
        raw["groups"]["robot"]["steps"].append({"workload": "camera"})
    elif mutation == "duplicate_group":
        raw["groups"]["robot"]["steps"].append({"group": "sensors"})
    elif mutation == "unknown":
        raw["groups"]["robot"]["steps"].append({"gate": "absent"})
    elif mutation == "badstep":
        raw["groups"]["robot"]["steps"].append({"parallel": []})
    else:
        del raw["workloads"]["camera"]["backend"]
    with pytest.raises(ConfigError):
        Config(raw)


def test_duplicate_yaml_and_scope_identity(config):
    with pytest.raises(ConfigError, match="Duplicate"):
        Config.parse("workloads: {}\nworkloads: {}")
    raw = config.json()
    raw["workloads"]["system_camera"] = dict(
        backend="systemd", scope="system", unit="camera.service"
    )
    Config(raw)
    raw["workloads"]["system_camera"]["scope"] = "user"
    with pytest.raises(ConfigError, match="Duplicate backend"):
        Config(raw)
    assert UnitKey("user", "camera.service") != UnitKey("system", "camera.service")


@pytest.mark.parametrize("success", [True, False])
async def test_http_gate(config, backend, success):
    app = web.Application()

    async def handle(request):
        return web.Response(status=204 if success else 503)

    app.router.add_get("/ready", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        raw = config.json()
        raw["gates"]["ready"] = dict(
            type="http",
            url=f"http://127.0.0.1:{port}/ready",
            expected_status=204,
            timeout_ms=100,
            interval_ms=10,
        )
        engine = Engine(Config(raw), backend)
        engine.launch("robot", True, "start")
        assert (await engine.wait())["status"] == ("completed" if success else "failed")
        assert len(backend.calls) == (4 if success else 2)
    finally:
        await runner.cleanup()


async def test_monitor_baseline_outage_dedup_and_no_repair(engine, backend):
    baseline = {
        key: observation(state="inactive", manager_generation=1) for key in engine.config.workloads
    }
    engine.update(copy.deepcopy(baseline))
    assert not engine.events
    baseline["detector"] = observation(state="active", invocation_id="one", manager_generation=1)
    engine.update(copy.deepcopy(baseline))
    assert any(event["kind"] == "gate_mismatch" for event in engine.events)
    count = len(engine.events)
    engine.update(copy.deepcopy(baseline))
    assert len(engine.events) == count
    baseline["detector"] = observation(error="unavailable")
    engine.update(copy.deepcopy(baseline))
    baseline["detector"] = observation(state="active", invocation_id="two", manager_generation=2)
    engine.update(baseline)
    assert len([e for e in engine.events if e["kind"] == "gate_mismatch"]) == 1
    assert any(e["kind"] == "observation_restored" for e in engine.events)
    assert backend.calls == []
