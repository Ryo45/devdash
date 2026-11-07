"""Remaining contracts from archive/rust-gpui:tests/core.rs."""

import asyncio
import json

import pytest
from aiohttp import web
from conftest import until

from devdash.engine import Engine
from devdash.model import Config, ConfigError


async def complete(engine):
    return await asyncio.wait_for(engine.wait(), 2)


async def test_immediate_cancel_does_not_issue_individual_operation(engine, backend):
    engine.launch("camera", False, "start")
    engine.request_cancel()
    run = await complete(engine)
    assert run["status"] == "cancelled"
    assert backend.calls == []
    assert [step["status"] for step in run["progress"]] == ["skipped"]


async def test_direct_restart_uses_backend_restart(engine, backend):
    engine.launch("detector", False, "restart")
    assert (await complete(engine))["status"] == "completed"
    assert backend.calls == [("detector", "restart")]
    assert not engine.approved


async def test_restart_cancel_during_stop_never_starts(engine, backend):
    backend.block["planner"] = asyncio.Event()
    engine.launch("robot", True, "restart")
    await until(lambda: backend.calls == [("planner", "stop")])
    engine.request_cancel()
    assert engine.busy and engine.run["status"] == "cancelling"
    backend.block["planner"].set()
    assert (await complete(engine))["status"] == "cancelled"
    assert backend.calls == [("planner", "stop")]


async def test_stop_failure_prevents_restart_start_phase(engine, backend):
    backend.fail.add("planner")
    engine.launch("robot", True, "restart")
    run = await complete(engine)
    assert run["status"] == "failed" and "planner" in run["error"]
    assert backend.calls == [("planner", "stop")]
    assert all(step["status"] == "skipped" for step in run["progress"][1:])


async def test_reinitialize_resets_only_selected_group_gates(config, backend):
    raw = config.json()
    raw["gates"]["independent_ready"] = {"type": "manual"}
    raw["groups"]["independent"] = {"steps": [{"gate": "independent_ready"}]}
    engine = Engine(Config(raw), backend)
    engine.approve("ready")
    engine.approve("independent_ready")
    engine.launch("robot", True, "reinitialize")
    await until(lambda: engine.run["waiting_gate"] == "ready")
    assert engine.approved == {"independent_ready"}
    engine.request_cancel()
    assert (await complete(engine))["status"] == "cancelled"


async def test_system_scope_operations_reach_backend(config, backend):
    raw = config.json()
    raw["workloads"]["camera"]["scope"] = "system"
    seen = []
    original = backend.operate

    async def operate(work, action, lease=None):
        seen.append((work["scope"], work["unit"], action))
        await original(work, action, lease)

    backend.operate = operate
    engine = Engine(Config(raw), backend)
    engine.launch("camera", False, "start")
    assert (await complete(engine))["status"] == "completed"
    assert seen == [("system", "camera.service", "start")]


async def test_restart_cancel_in_start_phase_skips_remainder(engine, backend):
    engine.approve("ready")
    original = backend.operate
    releases = {key: asyncio.Event() for key in ("camera", "imu")}

    async def operate(work, action, lease=None):
        key = work["unit"].removesuffix(".service")
        if action == "start" and key in releases:
            backend.block[key] = releases[key]
        await original(work, action, lease)

    backend.operate = operate
    engine.launch("robot", True, "restart")
    await until(lambda: len(backend.calls) == 6)
    engine.request_cancel()
    for event in releases.values():
        event.set()
    assert (await complete(engine))["status"] == "cancelled"
    assert len(backend.calls) == 6
    assert all(action == "stop" for _, action in backend.calls[:4])
    assert all(action == "start" for _, action in backend.calls[4:])


async def test_progress_tracks_steps_and_parallel_members_once(engine):
    engine.launch("robot", True, "start")
    await until(lambda: engine.run["waiting_gate"] == "ready")
    for label in ("camera", "imu", "parallel: camera, imu"):
        entries = [p for p in engine.run["progress"] if p["step"] == label]
        assert len(entries) == 1 and entries[0]["status"] == "completed"
    assert engine.run["progress"][-1]["step"] == "ready"
    assert engine.run["progress"][-1]["status"] == "running"
    assert not any(p["step"] == "detector" for p in engine.run["progress"])
    engine.approve("ready")
    run = await complete(engine)
    assert len(run["progress"]) == 6
    assert all(p["status"] == "completed" for p in run["progress"])


async def test_issued_job_failure_is_preserved_after_cancel(engine, backend):
    backend.block = {key: asyncio.Event() for key in ("camera", "imu")}
    backend.fail.add("camera")
    engine.launch("robot", True, "start")
    await until(lambda: len(backend.calls) == 2)
    engine.request_cancel()
    assert engine.run["status"] == "cancelling"
    for event in backend.block.values():
        event.set()
    run = await complete(engine)
    assert run["status"] == "failed"
    states = {p["step"]: p["status"] for p in run["progress"]}
    assert states["camera"] == "failed" and states["imu"] == "completed"
    assert states["detector"] == states["planner"] == "skipped"
    assert len(backend.calls) == 2 and "running" not in states.values()


@pytest.mark.parametrize(
    "text", ["broken", '{"boot":"test","approved":"ready"}', '{"boot":"test","approved":[false]}']
)
def test_persisted_approval_corruption_is_not_silently_discarded(config, backend, tmp_path, text):
    path = tmp_path / "approvals.json"
    path.write_text(text)
    with pytest.raises(ValueError):
        Engine(config, backend, path)


@pytest.mark.parametrize("kind", ["workload", "group", "gate"])
def test_unknown_references_and_multiple_step_kinds(config, kind):
    raw = config.json()
    raw["groups"]["robot"]["steps"].append({kind: "missing"})
    with pytest.raises(ConfigError, match="Unknown"):
        Config(raw)
    raw["groups"]["robot"]["steps"][-1] = {"workload": "planner", "group": "sensors"}
    with pytest.raises(ConfigError, match="Invalid Step"):
        Config(raw)
    assert Config.parse(json.dumps(config.json())).json() == config.json()


@pytest.fixture
async def http_gate(config, backend):
    state = {"status": 200, "calls": 0}

    async def handle(request):
        state["calls"] += 1
        return web.Response(status=state["status"])

    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    raw = config.json()
    raw["gates"]["ready"] = dict(
        type="http", url=f"http://127.0.0.1:{port}/", timeout_ms=150, interval_ms=10
    )
    try:
        yield Engine(Config(raw), backend), state
    finally:
        await runner.cleanup()


async def test_http_success_and_reevaluation(http_gate, backend):
    engine, state = http_gate
    engine.launch("robot", True, "start")
    assert (await complete(engine))["status"] == "completed"
    requests = state["calls"]
    state["status"] = 503
    backend.calls.clear()
    engine.launch("robot", True, "start")
    assert (await complete(engine))["status"] == "failed"
    assert state["calls"] > requests
    assert set(backend.calls) == {("camera", "start"), ("imu", "start")}


async def test_http_cancel(http_gate, backend):
    engine, state = http_gate
    state["status"] = 503
    engine.launch("robot", True, "start")
    await until(lambda: state["calls"] > 0)
    engine.request_cancel()
    assert (await complete(engine))["status"] == "cancelled"
    assert len(backend.calls) == 2


async def test_stop_skips_gate_and_reverses_hierarchy(engine, backend):
    engine.launch("robot", True, "stop")
    run = await complete(engine)
    assert run["status"] == "completed" and not engine.approved
    assert backend.calls[:2] == [("planner", "stop"), ("detector", "stop")]
    assert set(backend.calls[2:]) == {("camera", "stop"), ("imu", "stop")}
    assert next(p for p in run["progress"] if p["step"] == "ready")["status"] == "skipped"


async def test_cancel_gate_and_skip_following_no_rollback(engine, backend):
    engine.launch("robot", True, "start")
    await until(lambda: engine.run["waiting_gate"] == "ready")
    engine.request_cancel()
    run = await complete(engine)
    assert run["status"] == "cancelled"
    assert set(backend.calls) == {("camera", "start"), ("imu", "start")}
    assert all(
        p["status"] == "skipped" for p in run["progress"] if p["step"] in ("detector", "planner")
    )


async def test_parallel_cancel_drains_issued_jobs(engine, backend):
    backend.block = {key: asyncio.Event() for key in ("camera", "imu")}
    engine.launch("robot", True, "start")
    await until(lambda: len(backend.calls) == 2)
    engine.request_cancel()
    assert engine.run["status"] == "cancelling" and not engine.task.done()
    with pytest.raises(ValueError, match="Another"):
        engine.launch("planner", False, "start")
    for event in backend.block.values():
        event.set()
    assert (await complete(engine))["status"] == "cancelled"
    assert len(backend.calls) == 2
