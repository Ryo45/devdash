"""Fresh inspection remains separate from Stage dispatch."""

import asyncio

import pytest


async def test_cancel_during_inspection_issues_nothing(engine, backend):
    entered, release = asyncio.Event(), asyncio.Event()
    original = backend.dependencies

    async def inspect(seeds):
        entered.set()
        await release.wait()
        return await original(seeds)

    backend.dependencies = inspect
    engine.launch("sensors", True, "start")
    await entered.wait()
    engine.request_cancel()
    release.set()
    assert (await engine.wait())["status"] == "cancelled"
    assert not backend.calls


@pytest.mark.parametrize("failure", ["stale", "incomplete", "changed"])
async def test_uncertain_preflight_blocks_stage_but_not_direct_workload(engine, backend, failure):
    if failure == "stale":
        backend.graph.stale = True
    elif failure == "incomplete":
        from devdash.inspection import Graph

        async def missing(seeds):
            return Graph(incomplete=["manager metadata unavailable"])

        backend.dependencies = missing
    else:
        original = backend.dependencies

        async def changed(seeds):
            result = await original(seeds)
            backend.revision += 1
            return result

        backend.dependencies = changed
    engine.launch("sensors", True, "start")
    assert (await engine.wait())["status"] == "failed"
    assert not backend.calls
    engine.launch("camera", False, "start")
    assert (await engine.wait())["status"] == "completed"
    assert backend.calls == [("camera", "start")]


async def test_new_stage_operation_inspects_again_and_reports_invalidate(engine, backend):
    await engine.inspect("sensors", "start")
    backend.revision += 1
    assert engine.snapshot()["inspections"]["sensors"]["stale"]
    engine.launch("sensors", True, "start")
    assert (await engine.wait())["status"] == "completed"
