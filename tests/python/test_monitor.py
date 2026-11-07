"""Observations never initiate repair and unknown history stays unknown."""

import asyncio

from conftest import until

from devdash.model import observation


def test_observation_baseline_unattributed_changes_and_no_repeat_notifications(engine, backend):
    engine.approve("ready")
    for state in ("inactive", "active", "active", "not_found", "inactive"):
        engine.update({"detector": observation(state=state)})
    events = list(engine.events)
    assert len(events) == 3
    assert all(e["kind"] == "state_changed" for e in events)
    assert "origin unknown" in events[0]["message"]
    assert events[1]["level"] == "warning"
    assert "not_found → inactive" in events[2]["message"]
    assert "detector" in engine.config.workloads and not backend.calls


async def test_gate_mismatch_and_unexpected_change_during_operation_warn_once(engine, backend):
    engine.update({"detector": observation(state="active")})
    engine.update({"detector": observation(state="active")})
    assert [e["kind"] for e in engine.events] == ["gate_mismatch"]
    backend.block["planner"] = asyncio.Event()
    engine.launch("planner", False, "start")
    await until(lambda: bool(backend.calls))
    engine.update({"detector": observation(state="inactive")})
    assert engine.events[-1]["level"] == "warning"
    assert engine.events[-1]["kind"] == "state_changed"
    backend.block["planner"].set()
    await engine.wait()


def test_invocation_restart_and_bounded_log(engine):
    engine.approve("ready")
    for invocation in ("old", "new"):
        engine.update({"detector": observation(state="active", invocation_id=invocation)})
    assert len(engine.events) == 1 and engine.events[0]["kind"] == "state_changed"
    assert "invocation changed" in engine.events[0]["message"]
    for index in range(300):
        engine.emit("info", str(index))
    assert len(engine.events) == 200
    ids = [event["id"] for event in engine.events]
    assert ids == sorted(set(ids))
    assert engine.events[-1]["message"] == "299"


def test_observation_outage_does_not_clear_readiness_episode_or_infer_restart(engine):
    for state in ("active", "unknown", "active", "unknown", "active"):
        engine.update({"detector": observation(state=state)})
    kinds = [event["kind"] for event in engine.events]
    assert kinds.count("gate_mismatch") == 1
    assert kinds.count("observation_lost") == 2
    assert kinds.count("observation_restored") == 2
    assert "state_changed" not in kinds
    for state in ("inactive", "active"):
        engine.update({"detector": observation(state=state)})
    assert sum(e["kind"] == "gate_mismatch" for e in engine.events) == 2


def test_manager_generation_change_does_not_carry_old_invocation_into_new_baseline(engine):
    engine.approve("ready")
    for generation, invocation in ((1, "old"), (2, None), (2, "new")):
        engine.update(
            {
                "detector": observation(
                    state="active", manager_generation=generation, invocation_id=invocation
                )
            }
        )
    assert len(engine.events) == 1
    assert engine.events[0]["kind"] == "observation_restored"
    assert engine.known["detector"]["invocation_id"] == "new"


async def test_recent_request_does_not_claim_causality(engine, backend):
    engine.approve("ready")
    engine.update({"detector": observation(state="inactive")})
    backend.recent = lambda work: True
    engine.update({"detector": observation(state="active")})
    assert "causality unconfirmed" in engine.events[-1]["message"]
    assert engine.events[-1]["correlation"] == "unknown" and not backend.calls
