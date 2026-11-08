"""Safety guarantees ported from cancelled/failed Group workflows to Stage batches."""

import asyncio

import pytest
from conftest import until

from devdash.model import Config, ConfigError


async def test_immediate_cancel_skips_all_unissued_requests(engine, backend):
    engine.launch("sensors", True, "start")
    engine.request_cancel()
    assert (await engine.wait())["status"] == "cancelled"
    assert not backend.calls
    assert all(p["status"] == "skipped" for p in engine.run["progress"])


async def test_cancel_does_not_hide_failure_of_already_dispatched_work(engine, backend):
    backend.fail.add("camera")
    backend.block["camera"] = asyncio.Event()
    engine.launch("sensors", True, "start")
    await until(lambda: len(backend.calls) == 2)
    engine.request_cancel()
    backend.block["camera"].set()
    assert (await engine.wait())["status"] == "failed"
    assert len(backend.calls) == 2


async def test_stage_restart_uses_each_backend_restart_not_stop_start_workflow(engine, backend):
    engine.launch("sensors", True, "restart")
    assert (await engine.wait())["status"] == "completed"
    assert set(backend.calls) == {("camera", "restart"), ("imu", "restart")}


async def test_invalid_next_and_removed_actions_issue_nothing(engine, backend):
    for action in ("reinitialize", "approve", "invalid"):
        with pytest.raises(ValueError):
            engine.launch("sensors", True, action)
    with pytest.raises(ValueError, match="No next"):
        engine.launch_next("control")
    assert not backend.calls


def test_duplicate_yaml_and_unknown_fields_still_rejected():
    for text in ("stages: []\nstages: []", "unexpected: true", "workloads: {a: {backend: bad}}"):
        with pytest.raises(ConfigError):
            Config.parse(text)
