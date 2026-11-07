"""Inspection is an input snapshot, never an execution DAG."""

import asyncio
import copy

import pytest

from devdash.model import UnitKey


async def test_reinspected_before_every_operation_and_blocked_before_jobs(engine, backend):
    assert not (await engine.inspect("robot", "start"))["diagnostics"]
    backend.graph.edges.add(
        (UnitKey("user", "camera.service"), "Requires", UnitKey("user", "detector.service"))
    )
    engine.launch("robot", True, "start")
    assert (await engine.wait())["status"] == "failed"
    assert not backend.calls
    backend.graph.edges.clear()
    engine.launch("perception", True, "start")
    assert (await engine.wait())["status"] == "completed"
    assert backend.calls == [("detector", "start")]


@pytest.fixture
def paused_inspection(backend):
    entered, resume = asyncio.Event(), asyncio.Event()

    async def dependencies(seeds):
        snapshot = copy.deepcopy(backend.graph)
        snapshot.inspected |= seeds
        entered.set()
        await resume.wait()
        return snapshot

    backend.dependencies = dependencies
    return entered, resume


async def test_cancel_during_preflight_preserves_approvals_and_issues_no_jobs(
    engine, backend, paused_inspection
):
    entered, resume = paused_inspection
    engine.approve("ready")
    engine.launch("robot", True, "reinitialize")
    await asyncio.wait_for(entered.wait(), 2)
    engine.request_cancel()
    resume.set()
    assert (await engine.wait())["status"] == "cancelled"
    assert engine.approved == {"ready"} and not backend.calls


async def test_invalidated_refresh_cannot_publish_old_snapshot_as_fresh(
    engine, backend, paused_inspection
):
    entered, resume = paused_inspection
    resume.set()
    await engine.inspect("robot", "start")
    resume.clear()
    entered.clear()
    refresh = asyncio.create_task(engine.inspect("robot", "start"))
    await asyncio.wait_for(entered.wait(), 2)
    engine.invalidate()
    backend.graph.edges.add(
        (UnitKey("user", "camera.service"), "Requires", UnitKey("user", "detector.service"))
    )
    resume.set()
    report = await refresh
    assert report["stale"] and engine.snapshot()["inspections"]["robot"]["stale"]
    fresh = await engine.inspect("robot", "start")
    assert not fresh["stale"]
    assert any(d["code"] == "gate_bypass" for d in fresh["diagnostics"])


async def test_approval_changes_invalidate_cached_inspection(engine):
    await engine.inspect("robot", "start")
    assert not engine.snapshot()["inspections"]["robot"]["stale"]
    engine.approve("ready")
    assert engine.snapshot()["inspections"]["robot"]["stale"]


async def test_operation_inspection_seeds_only_selected_scope(engine, backend):
    seen = []
    original = backend.dependencies

    async def dependencies(seeds):
        seen.append(set(seeds))
        return await original(seeds)

    backend.dependencies = dependencies
    report = await engine.inspect("perception", "start")
    assert seen == [{UnitKey("user", "detector.service")}]
    assert report["requested_units"] == [{"scope": "user", "unit": "detector.service"}]


@pytest.mark.parametrize("change", ["inputs", "manager_revision", "approval"])
async def test_changed_preflight_is_refused_before_jobs(engine, backend, paused_inspection, change):
    entered, resume = paused_inspection
    engine.launch("robot", True, "start")
    await asyncio.wait_for(entered.wait(), 2)
    if change == "inputs":
        engine.invalidate()
    elif change == "manager_revision":
        backend.revision += 1
    else:
        engine.approve("ready")
    resume.set()
    assert (await engine.wait())["status"] == "failed"
    assert "stale" in engine.run["error"] and not backend.calls
