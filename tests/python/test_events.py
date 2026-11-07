"""Restored tests/events.rs against real private buses, never host systemd."""

import asyncio

import pytest
from conftest import until
from private_bus import Harness

from devdash.adapters.systemd import PATH, SERVICE


@pytest.fixture
async def bus_fixture(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    try:
        yield harness
    finally:
        await harness.close()


@pytest.mark.parametrize("early", [True, False])
async def test_delayed_reply_is_correlated_without_false_external_warning(bus_fixture, early):
    h = await bus_fixture.start()
    manager = h.managers["user"]
    manager.early = early
    release = manager.pause("reply")
    h.engine.launch("probe", False, "start")
    await asyncio.wait_for(manager.entered["reply"].wait(), 2)
    assert h.engine.busy
    release.set()
    assert (await asyncio.wait_for(h.engine.wait(), 2))["status"] == "completed"
    jobs = [event for event in h.engine.events if event["kind"] == "job"]
    assert len(jobs) == 1
    assert jobs[0]["correlation"] == "requested" and jobs[0]["level"] == "info"
    assert jobs[0]["job_result"] == "done"


async def test_system_authorization_and_same_named_units_use_separate_managers(bus_fixture):
    h = await bus_fixture.start()
    h.managers["system"].deny = True
    h.engine.launch("system_probe", False, "start")
    run = await asyncio.wait_for(h.engine.wait(), 2)
    assert run["status"] == "failed" and "Polkit" in run["error"] and "AccessDenied" in run["error"]
    assert h.managers["system"].calls == [("StartUnit", "same.service")]
    assert not h.managers["user"].calls
    h.engine.launch("probe", False, "start")
    assert (await asyncio.wait_for(h.engine.wait(), 2))["status"] == "completed"
    assert h.managers["user"].calls == [("StartUnit", "same.service")]
    assert len(h.managers["system"].calls) == 1  # No denial retry.


async def test_manager_owner_loss_finishes_pending_operation_and_releases_busy(bus_fixture):
    h = await bus_fixture.start()
    manager = h.managers["user"]
    manager.hold_job = True
    h.engine.launch("probe", False, "start")
    await until(lambda: bool(manager.calls))
    h.engine.request_cancel()
    await manager.bus.release_name(SERVICE)
    run = await asyncio.wait_for(h.engine.wait(), 2)
    assert run["status"] == "failed" and "outcome unknown" in run["error"]
    assert not h.engine.busy and not h.backend.health()["user"]["available"]
    assert any(e["kind"] == "disconnected" for e in h.engine.events)
    h.engine.launch("probe", False, "start")
    assert (await asyncio.wait_for(h.engine.wait(), 2))["status"] == "failed"
    assert len(manager.calls) == 1


async def test_burst_of_more_than_64_events_does_not_stall_control(bus_fixture):
    h = await bus_fixture.start()
    h.managers["user"].burst = 300
    h.engine.launch("probe", False, "start")
    assert (await asyncio.wait_for(h.engine.wait(), 3))["status"] == "completed"
    assert h.backend.health()["user"]["available"]
    assert h.engine.monitor_gap is None
    assert any(e["correlation"] == "requested" for e in h.engine.events)


async def test_events_during_initial_baseline_are_buffered(bus_fixture):
    h = await bus_fixture.start()
    manager = h.managers["user"]
    release = manager.pause("read")
    poll = asyncio.create_task(h.engine.poll())
    try:
        await asyncio.wait_for(manager.entered["read"].wait(), 2)
        await manager.job(91)
        await until(lambda: any(e["job_path"] == f"{PATH}/job/91" for e in h.engine.events))
        assert not poll.done()
        release.set()
        await asyncio.wait_for(poll, 2)
        assert len([e for e in h.engine.events if e["kind"] == "job"]) == 1
    finally:
        release.set()
        await asyncio.gather(poll, return_exceptions=True)


async def test_slow_poll_does_not_delay_events_and_reload_warns_during_operation(bus_fixture):
    h = await bus_fixture.start()
    await h.engine.poll()
    manager = h.managers["user"]
    release = manager.pause("read")
    poll = asyncio.create_task(h.engine.poll())
    try:
        await asyncio.wait_for(manager.entered["read"].wait(), 2)
        await manager.job(92)
        await until(lambda: any(e["job_path"] == f"{PATH}/job/92" for e in h.engine.events))
        assert not poll.done()
        manager.hold_job = True
        h.engine.launch("probe", False, "start")
        await until(lambda: bool(manager.calls))
        await manager.signal("Reloading", "b", [True])
        await manager.signal("Reloading", "b", [False])
        await until(lambda: any(e["kind"] == "reloaded" for e in h.engine.events))
        assert all(
            e["level"] == "warning"
            for e in h.engine.events
            if e["kind"] in ("reloading", "reloaded")
        )
        await manager.job(1)
        assert (await asyncio.wait_for(h.engine.wait(), 2))["status"] == "completed"
    finally:
        release.set()
        await asyncio.gather(poll, return_exceptions=True)


async def test_replacement_owner_resubscribes_before_accepting_new_work_and_rebaselines(
    bus_fixture,
):
    h = await bus_fixture.start()
    await h.engine.poll()
    old = h.backend.session("user")
    await h.managers["user"].bus.release_name(SERVICE)
    await until(lambda: old.lost.is_set())
    replacement = await h.peer("user", named=False)
    release = replacement.pause("subscribe")
    await replacement.bus.request_name(SERVICE)
    await asyncio.wait_for(replacement.entered["subscribe"].wait(), 5)
    try:
        assert not h.backend.health()["user"]["available"]
        h.engine.launch("probe", False, "start")
        assert (await asyncio.wait_for(h.engine.wait(), 2))["status"] == "failed"
        assert not replacement.calls
    finally:
        release.set()
    await until(lambda: h.backend.health()["user"]["available"], timeout=6)
    assert replacement.subscriptions == 1
    replacement.active["same.service"] = True
    await h.engine.poll()
    assert any(e["kind"] == "observation_restored" for e in h.engine.events)
    assert h.engine.monitor_gap
    h.engine.launch("probe", False, "start")
    assert (await asyncio.wait_for(h.engine.wait(), 2))["status"] == "completed"
    assert replacement.calls == [("StartUnit", "same.service")]


async def test_disconnected_bus_is_reopened_at_same_address_without_replay(bus_fixture):
    h = await bus_fixture.start()
    await h.engine.poll()
    h.managers["user"].hold_job = True
    old = h.backend.session("user")
    h.engine.launch("probe", False, "start")
    await until(lambda: bool(h.managers["user"].calls))
    await h.buses["user"].close()
    assert (await asyncio.wait_for(h.engine.wait(), 2))["status"] == "failed"
    await h.buses["user"].start()
    replacement = await h.peer("user")
    await until(lambda: h.backend.health()["user"]["available"], timeout=8)
    assert old.lost.is_set() and h.backend.session("user") is not old
    assert replacement.subscriptions == 1 and not replacement.calls
    await h.engine.poll()
    assert any(e["kind"] == "observation_restored" for e in h.engine.events)
    h.engine.launch("probe", False, "start")
    assert (await asyncio.wait_for(h.engine.wait(), 2))["status"] == "completed"


async def test_managers_absent_at_startup_recover_without_restarting_core(bus_fixture):
    h = await bus_fixture.start(named=False)
    assert not any(health["available"] for health in h.backend.health().values())
    for manager in h.managers.values():
        await manager.bus.request_name(SERVICE)
    await until(
        lambda: all(health["available"] for health in h.backend.health().values()), timeout=8
    )
    h.engine.launch("system_probe", False, "start")
    assert (await asyncio.wait_for(h.engine.wait(), 2))["status"] == "completed"
    assert not h.managers["user"].calls


async def test_group_waiting_at_gate_is_not_resumed_after_manager_replacement(bus_fixture):
    h = await bus_fixture.start()
    h.engine.launch("robot", True, "start")
    await until(lambda: h.engine.run["waiting_gate"] == "ready")
    await h.managers["user"].bus.release_name(SERVICE)
    assert (await asyncio.wait_for(h.engine.wait(), 2))["status"] == "failed"
    replacement = await h.peer("user")
    await until(lambda: h.backend.health()["user"]["available"], timeout=8)
    h.engine.approve("ready")
    await h.engine.poll()
    assert h.engine.run["status"] == "failed" and not replacement.calls
    assert all(call[1] != "after.service" for peer in h.peers for call in peer.calls)


async def test_repeated_registered_unit_metadata_signals_do_not_displace_jobs(bus_fixture):
    h = await bus_fixture.start()
    for _ in range(100):
        await h.managers["user"].signal("UnitNew", "so", ["same.service", PATH + "/unit/probe"])
    await h.managers["user"].job(100)
    await until(lambda: any(e["job_path"] == f"{PATH}/job/100" for e in h.engine.events))
    assert sum(e["kind"] == "unit_new" for e in h.engine.events) == 1
    assert h.engine.monitor_gap is None


async def test_manager_loss_during_operation_preparation_does_not_start(bus_fixture):
    h = await bus_fixture.start()
    manager = h.managers["user"]
    release = manager.pause("load")
    h.engine.launch("probe", False, "start")
    await asyncio.wait_for(manager.entered["load"].wait(), 2)
    try:
        await manager.bus.release_name(SERVICE)
        run = await asyncio.wait_for(h.engine.wait(), 2)
        assert run["status"] == "failed" and not h.engine.busy
        assert "outcome unknown" in run["error"] and not manager.calls
    finally:
        release.set()
