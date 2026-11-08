"""Stage replacement for old coordinator contracts: explicit requests, no durable progress."""

import asyncio

import pytest
from conftest import until

from devdash.engine import Engine
from devdash.model import Config, ConfigError


async def test_stage_start_waits_for_both_parallel_results_and_next_is_explicit(engine, backend):
    release = backend.block["camera"] = asyncio.Event()
    engine.launch("sensors", True, "start")
    await until(lambda: len(backend.calls) == 2)
    assert engine.busy
    assert set(backend.calls) == {("camera", "start"), ("imu", "start")}
    release.set()
    assert (await engine.wait())["status"] == "completed"
    assert len(backend.calls) == 2
    engine.launch_next("sensors")
    assert (await engine.wait())["status"] == "completed"
    assert backend.calls[-1] == ("detector", "start")
    assert ("planner", "start") not in backend.calls


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
async def test_only_selected_stage_is_operated(engine, backend, action):
    engine.launch("perception", True, action)
    assert (await engine.wait())["status"] == "completed"
    assert backend.calls == [("detector", action)]


async def test_partial_failure_drains_every_result_without_rollback(engine, backend):
    backend.fail.add("camera")
    backend.block["imu"] = asyncio.Event()
    engine.launch("sensors", True, "start")
    await until(lambda: len(backend.calls) == 2)
    assert engine.busy
    backend.block["imu"].set()
    run = await engine.wait()
    assert run["status"] == "failed"
    assert {p["workload"]: p["status"] for p in run["progress"]} == {
        "camera": "failed",
        "imu": "completed",
    }
    assert len(backend.calls) == 2


async def test_cancel_drains_dispatched_requests_and_does_not_start_another_stage(engine, backend):
    backend.block["camera"] = asyncio.Event()
    engine.launch("sensors", True, "start")
    await until(lambda: len(backend.calls) == 2)
    engine.request_cancel()
    assert engine.run["status"] == "cancelling" and engine.busy
    with pytest.raises(ValueError, match="Another"):
        engine.launch_next("sensors")
    backend.block["camera"].set()
    assert (await engine.wait())["status"] == "cancelled"
    assert all(p["status"] == "completed" for p in engine.run["progress"])
    assert len(backend.calls) == 2


async def test_direct_restart_is_backend_restart_and_new_core_has_no_history(config, backend):
    engine = Engine(config, backend)
    engine.launch("camera", False, "restart")
    assert (await engine.wait())["status"] == "completed"
    assert backend.calls == [("camera", "restart")]
    recovered = Engine(config, backend)
    assert recovered.run["status"] == "" and not recovered.observations
    assert not hasattr(recovered, "approved") and not hasattr(recovered, "gate")
    await recovered.poll()
    assert recovered.observations["camera"]["state"] == "active"
    assert len(backend.calls) == 1


@pytest.mark.parametrize(
    "change", ["duplicate", "unknown", "same", "reverse", "cycle", "cross", "docker", "old"]
)
def test_stage_and_dependency_validation(config, change):
    raw = config.json()
    if change == "duplicate":
        raw["stages"][1]["workloads"].append("camera")
    elif change == "unknown":
        raw["stages"][0]["workloads"].append("missing")
    elif change == "same":
        raw["dependencies"] = {"camera": ["imu"]}
    elif change == "reverse":
        raw["dependencies"] = {"camera": ["detector"]}
    elif change == "cycle":
        raw["dependencies"] = {"camera": ["detector"], "detector": ["camera"]}
    elif change == "cross":
        raw["workloads"]["camera"]["scope"] = "system"
        raw["dependencies"] = {"detector": ["camera"]}
    elif change == "docker":
        raw["workloads"]["camera"] = {"backend": "docker", "docker": {"container": "camera"}}
        raw["dependencies"] = {"detector": ["camera"]}
    else:
        raw["groups"] = {}
    with pytest.raises(ConfigError):
        Config(raw)
