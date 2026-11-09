"""Recovery is a new current-state Run, with no reference to prior Plan results."""

import asyncio
import copy

import pytest
from conftest import until
from test_plans import checkpoint, proceed

from devdash.engine import Engine
from devdash.model import Config, observation, readiness
from devdash.start_missing import assess


@pytest.fixture
def missing(config, backend):
    raw = config.json()
    raw["stages"][0]["checks"] = ["Check camera and IMU"]
    raw["stages"][1]["checks"] = ["Check perception"]
    raw["plan_templates"] = {"recover": {"kind": "start_missing"}}
    return Engine(Config(raw), backend)


async def preview(engine):
    await engine.poll()
    return engine.plans.preview(template="recover")


def decisions(plan):
    return {key: value for step in plan["steps"] for key, value in step.get("preview", {}).items()}


@pytest.mark.parametrize(
    "values,action",
    [
        ({"state": "inactive"}, "start"),
        ({"state": "failed"}, "start"),
        ({"state": "active"}, "skip"),
        ({"state": "active", "health": None}, "skip"),
        ({"state": "active", "health": "unhealthy"}, "blocked"),
        ({"state": "active", "health": "starting"}, "blocked"),
        ({"state": "active", "readiness": readiness("not_ready", "probe")}, "blocked"),
        ({"state": "active", "readiness": readiness("unknown", "systemd notify")}, "blocked"),
        ({"state": "active", "observer_error": "offline"}, "blocked"),
        ({"state": "active", "stale": True}, "blocked"),
        ({"state": "inactive", "stale": True}, "blocked"),
        ({"state": "unknown"}, "blocked"),
        ({"state": "not_found"}, "blocked"),
        ({"state": "activating"}, "blocked"),
        ({"state": "deactivating"}, "blocked"),
    ],
)
def test_current_state_policy(values, action):
    value = observation(**values)
    value.setdefault("stale", False)
    assert assess({"backend": "systemd"}, value)["action"] == action


def test_docker_and_http_contracts_are_not_inferred_from_systemd_active():
    work = dict(backend="systemd", observer={"docker": {"container": "robot"}})
    value = observation(state="active", stale=False)
    assert assess(work, value)["action"] == "blocked"
    value.update(readiness=readiness("ready", "docker running"))
    assert assess(work, value)["action"] == "skip"  # No Healthcheck is not unhealthy.
    work["observer"] = {"http": {"url": "http://example.invalid"}}
    assert assess(work, value)["action"] == "blocked"
    value["http"] = {"state": "ready"}
    assert assess(work, value)["action"] == "skip"


async def test_all_inactive_preserves_startup_order_and_checkpoints(missing, backend):
    plan = await preview(missing)
    assert all(d["action"] == "start" for d in decisions(plan).values())
    assert not backend.calls and not missing.busy
    missing.plans.start(plan["id"])
    await proceed(missing, "sensors")
    await proceed(missing, "perception")
    result = await missing.wait()
    assert result["status"] == "completed" and missing.plans.active is None
    assert backend.calls == [
        ("camera", "start"),
        ("imu", "start"),
        ("detector", "start"),
        ("planner", "start"),
    ]


async def test_partial_failure_then_new_current_state_plan_recovers(missing, backend):
    backend.observations.update(
        camera=observation(state="active", invocation_id="camera"), imu=observation(state="failed")
    )
    backend.fail.add("imu")
    plan = await preview(missing)
    assert decisions(plan)["camera"]["action"] == "skip"
    assert decisions(plan)["imu"]["action"] == "start"
    first = missing.plans.start(plan["id"])
    failed = await missing.wait()
    assert failed["status"] == "failed" and "fake failed: imu" in failed["error"]
    assert missing.plans.active is None and backend.calls == [("imu", "start")]
    assert backend.observations["camera"]["state"] == "active"
    backend.fail.clear()
    # Recovery does not need even the last display report.
    missing.operator.run.clear()
    second = missing.plans.start((await preview(missing))["id"])
    assert second != first
    await proceed(missing, "sensors")
    await proceed(missing, "perception")
    assert (await missing.wait())["status"] == "completed"
    assert backend.calls == [
        ("imu", "start"),
        ("imu", "start"),
        ("detector", "start"),
        ("planner", "start"),
    ]
    missing.launch("detector", False, "restart")
    assert (await missing.wait())["status"] == "completed"


async def test_all_active_still_requires_new_run_local_checkpoints(missing, backend):
    backend.observations = {key: observation(state="active") for key in missing.config.workloads}
    old = None
    for _ in range(2):
        missing.plans.start((await preview(missing))["id"])
        run = await checkpoint(missing, "sensors")
        if old:
            with pytest.raises(ValueError, match="changed|ended"):
                await missing.plans.continue_run(old["id"], old["checkpoint"]["token"])
        await proceed(missing, "sensors")
        await proceed(missing, "perception")
        result = await missing.wait()
        assert result["status"] == "completed" and result["progress"] == []
        assert not backend.calls
        old = run


@pytest.mark.parametrize("state", ["unknown", "activating", "deactivating"])
async def test_uncertain_execution_blocks_even_without_checks(missing, backend, state):
    for stage in missing.config.stages:
        stage["checks"] = []
    backend.observations["camera"] = observation(state=state)
    plan = await preview(missing)
    assert decisions(plan)["camera"]["action"] == "blocked"
    missing.plans.start(plan["id"])
    assert (await missing.wait())["status"] == "failed"
    assert backend.calls == [] and missing.plans.active is None


async def test_preview_is_prediction_and_execute_reduces_unnecessary_requests(missing, backend):
    plan = await preview(missing)
    backend.observations["camera"] = observation(state="active")
    missing.plans.start(plan["id"])
    run = await checkpoint(missing, "sensors")
    step = run["steps"][0]
    assert step["preview"]["camera"]["action"] == "start"
    assert step["assessment"]["camera"]["action"] == "skip"
    assert backend.calls == [("imu", "start")]
    missing.plans.cancel(run["id"])
    await missing.wait()


@pytest.mark.parametrize("change", ["inactive", "new_generation", "unhealthy"])
async def test_loss_or_replacement_of_preview_active_state_fails_without_restart(
    missing, backend, change
):
    backend.observations["camera"] = observation(state="active", invocation_id="old")
    plan = await preview(missing)
    value = backend.observations["camera"]
    value.update(
        {"state": "inactive"}
        if change == "inactive"
        else {"health": "unhealthy"}
        if change == "unhealthy"
        else {"invocation_id": "new"}
    )
    missing.plans.start(plan["id"])
    assert (await missing.wait())["status"] == "failed"
    assert not backend.calls


async def test_empty_checks_cannot_bypass_bad_post_start_readiness(missing, backend):
    for stage in missing.config.stages:
        stage["checks"] = []
    original = backend.operate

    async def operate(work, *args):
        await original(work, *args)
        if work["unit"] == "imu.service":
            backend.observations["imu"]["readiness"] = readiness("not_ready", "docker health")

    backend.operate = operate
    missing.plans.start((await preview(missing))["id"])
    result = await missing.wait()
    assert result["status"] == "failed" and "not_ready" in result["error"]
    assert all(row["status"] == "completed" for row in result["progress"])
    assert backend.calls == [("camera", "start"), ("imu", "start")]


async def test_stage_rechecks_after_inspection_and_checkpoint(missing, backend):
    missing.plans.start((await preview(missing))["id"])
    await proceed(missing, "sensors")
    await checkpoint(missing, "perception")
    # A fresh token alone cannot bypass unhealthy current prerequisites.
    backend.observations["camera"]["health"] = "unhealthy"
    await missing.poll()
    run = copy.deepcopy(missing.snapshot()["plan"])
    with pytest.raises(RuntimeError, match="Health unhealthy"):
        await missing.plans.continue_run(run["id"], run["checkpoint"]["token"])
    assert ("planner", "start") not in backend.calls
    missing.plans.cancel(run["id"])
    await missing.wait()


async def test_cancel_drains_and_then_normal_operations_are_free(missing, backend):
    release = backend.block["imu"] = asyncio.Event()
    backend.observations["camera"] = observation(state="active")
    run_id = missing.plans.start((await preview(missing))["id"])
    await until(lambda: backend.calls == [("imu", "start")])
    missing.plans.cancel(run_id)
    assert missing.busy
    release.set()
    assert (await missing.wait())["status"] == "cancelled"
    missing.launch("camera", False, "restart")
    assert (await missing.wait())["status"] == "completed"


async def test_observation_failure_after_job_is_not_job_success_evidence(missing, backend):
    original = backend.operate

    async def operate(work, *args):
        await original(work, *args)
        backend.observations["camera"] = observation(error="lost observation")

    backend.operate = operate
    missing.plans.start((await preview(missing))["id"])
    result = await missing.wait()
    assert result["status"] == "failed" and all(
        row["status"] == "completed" for row in result["progress"]
    )
    assert ("detector", "start") not in backend.calls


async def test_manager_loss_and_core_restart_never_resume_missing_run(missing, backend):
    missing.plans.start((await preview(missing))["id"])
    await checkpoint(missing, "sensors")
    backend.lost.set()
    result = await asyncio.wait_for(missing.wait(), 2)
    assert result["status"] == "failed" and "unknown" in result["error"]
    calls = list(backend.calls)
    restored = Engine(missing.config, backend)
    await restored.poll()
    assert restored.plans.active is None and not restored.plans.previews
    assert backend.calls == calls


async def test_state_rechecked_after_slow_inspection_before_dispatch(missing, backend):
    plan = await preview(missing)
    original = backend.dependencies

    async def dependencies(seeds):
        result = await original(seeds)
        backend.observations["camera"] = observation(state="activating")
        return result

    backend.dependencies = dependencies
    missing.plans.start(plan["id"])
    result = await missing.wait()
    assert result["status"] == "failed" and "activating" in result["error"]
    assert not backend.calls


async def test_blocked_preview_requires_new_review_even_after_observation_recovers(
    missing, backend
):
    backend.observations["camera"] = observation(state="unknown")
    plan = await preview(missing)
    backend.observations["camera"] = observation(state="inactive")
    missing.plans.start(plan["id"])
    result = await missing.wait()
    assert result["status"] == "failed" and "preview a new Plan" in result["error"]
    assert not backend.calls
