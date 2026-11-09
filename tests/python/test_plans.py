"""Run-local Plans share one Operator; confirmations never become durable policy."""

import asyncio
import copy

import pytest
from conftest import Fake, until

from devdash.engine import Engine
from devdash.model import Config, observation
from devdash.observer import Observer
from devdash.plans import steps_for


@pytest.fixture
def planned(config, backend):
    raw = config.json()
    raw["stages"][0]["checks"] = ["Check camera images and IMU"]
    raw["stages"][1]["checks"] = ["Check perception output"]
    raw["plan_templates"] = {
        "boot": {"kind": "startup"},
        "perception": {"kind": "restart_from", "from_stage": "perception"},
    }
    return Engine(Config(raw), backend)


async def checkpoint(engine, stage):
    await until(
        lambda: (
            engine.snapshot()["plan"] is not None
            and (engine.snapshot()["plan"]["checkpoint"] or {}).get("stage") == stage
        )
    )
    return copy.deepcopy(engine.snapshot()["plan"])


async def proceed(engine, stage):
    run = await checkpoint(engine, stage)
    await engine.plans.continue_run(run["id"], run["checkpoint"]["token"])
    return run


def test_preview_is_inert_and_templates_are_static(planned, backend):
    preview = planned.plans.preview(template="boot")
    assert not planned.busy and backend.calls == []
    assert [s.get("action", "checkpoint") for s in preview["steps"]] == [
        "start",
        "checkpoint",
        "start",
        "checkpoint",
        "start",
    ]
    assert planned.snapshot()["plan"] is None
    assert planned.config.json()["plan_templates"]["boot"] == {"kind": "startup"}
    for _ in range(12):
        planned.plans.preview("startup")
    assert len(planned.plans.previews) == 8


async def test_normal_operator_then_startup_checkpoints_then_free_operations(planned, backend):
    planned.launch("detector", False, "restart")
    assert (await planned.wait())["status"] == "completed"
    assert planned.plans.active is None and not planned.plans.previews
    preview = planned.plans.preview(template="boot")
    run_id = planned.plans.start(preview["id"])
    sensors = await checkpoint(planned, "sensors")
    assert sensors["id"] == run_id and backend.calls == [
        ("detector", "restart"),
        ("camera", "start"),
        ("imu", "start"),
    ]
    with pytest.raises(ValueError, match="Another"):
        planned.launch("planner", False, "start")
    await proceed(planned, "sensors")
    await checkpoint(planned, "perception")
    assert backend.calls[-1] == ("detector", "start") and ("planner", "start") not in backend.calls
    await proceed(planned, "perception")
    result = await planned.wait()
    assert result["status"] == "completed" and planned.plans.active is None
    assert result["plan_result"]["id"] == run_id
    assert "token" not in str(result["plan_result"]) and "leases" not in str(result["plan_result"])
    assert backend.calls[-1] == ("planner", "start")
    assert not hasattr(planned.operator, "approved")
    planned.launch("detector", False, "stop")
    assert (await planned.wait())["status"] == "completed"
    assert "plan_result" not in planned.run  # Display history never restricts later operations.


async def test_reexecuting_same_template_does_not_reuse_confirmation(planned):
    first = planned.plans.start(planned.plans.preview(template="boot")["id"])
    old = await proceed(planned, "sensors")
    await proceed(planned, "perception")
    await planned.wait()
    second = planned.plans.start(planned.plans.preview(template="boot")["id"])
    run = await checkpoint(planned, "sensors")
    assert second != first and run["checkpoint"]["token"] != old["checkpoint"]["token"]
    with pytest.raises(ValueError, match="changed|ended"):
        await planned.plans.continue_run(first, old["checkpoint"]["token"])
    planned.plans.cancel(second)
    assert (await planned.wait())["status"] == "cancelled" and planned.plans.active is None


async def test_restart_from_suffix_and_stop_from_suffix_order(planned, backend):
    preview = planned.plans.preview(template="perception")
    assert [(s["stage"], s.get("action", "checkpoint")) for s in preview["steps"]] == [
        ("control", "stop"),
        ("perception", "stop"),
        ("perception", "start"),
        ("perception", "checkpoint"),
        ("control", "start"),
    ]
    planned.plans.start(preview["id"])
    await proceed(planned, "perception")
    assert (await planned.wait())["status"] == "completed"
    assert backend.calls == [
        ("planner", "stop"),
        ("detector", "stop"),
        ("detector", "start"),
        ("planner", "start"),
    ]
    backend.calls.clear()
    planned.plans.start(planned.plans.preview("stop_from", "perception")["id"])
    assert (await planned.wait())["status"] == "completed"
    assert backend.calls == [("planner", "stop"), ("detector", "stop")]


async def test_abort_checkpoint_releases_slot_and_dispatched_state_remains(planned, backend):
    run_id = planned.plans.start(planned.plans.preview("startup")["id"])
    await checkpoint(planned, "sensors")
    planned.plans.cancel(run_id)
    assert (await planned.wait())["status"] == "cancelled"
    assert planned.snapshot()["plan"] is None and not planned.busy
    assert backend.observations["camera"]["state"] == "active"
    assert backend.calls == [("camera", "start"), ("imu", "start")]
    planned.launch("planner", False, "restart")
    assert (await planned.wait())["status"] == "completed"


async def test_abort_drains_dispatched_operations_without_issuing_later_steps(planned, backend):
    release = backend.block["camera"] = asyncio.Event()
    run_id = planned.plans.start(planned.plans.preview("startup")["id"])
    await until(lambda: len(backend.calls) == 2)
    planned.plans.cancel(run_id)
    assert planned.busy and planned.run["status"] == "cancelling"
    release.set()
    result = await planned.wait()
    assert result["status"] == "cancelled" and backend.calls == [
        ("camera", "start"),
        ("imu", "start"),
    ]
    assert planned.plans.active is None


async def test_abort_during_suffix_stop_never_begins_start_phase(planned, backend):
    release = backend.block["planner"] = asyncio.Event()
    run_id = planned.plans.start(planned.plans.preview("restart_from", "perception")["id"])
    await until(lambda: backend.calls == [("planner", "stop")])
    planned.plans.cancel(run_id)
    assert planned.busy and planned.run["status"] == "cancelling"
    release.set()
    result = await planned.wait()
    assert result["status"] == "cancelled" and backend.calls == [("planner", "stop")]
    assert result["plan_result"]["steps"][0]["status"] == "completed"
    assert all(s["status"] == "skipped" for s in result["plan_result"]["steps"][1:])
    assert planned.plans.active is None


async def test_partial_failure_keeps_success_and_releases_run(planned, backend):
    backend.fail.add("imu")
    planned.plans.start(planned.plans.preview("startup")["id"])
    result = await planned.wait()
    assert result["status"] == "failed" and planned.plans.active is None
    assert {p["workload"]: p["status"] for p in result["progress"]} == {
        "camera": "completed",
        "imu": "failed",
    }
    assert all(s["status"] == "skipped" for s in result["plan_result"]["steps"][1:])
    assert backend.observations["camera"]["state"] == "active"
    assert len(backend.calls) == 2


async def test_generation_change_invalidates_displayed_confirmation(planned, backend):
    planned.plans.start(planned.plans.preview("startup")["id"])
    run = await checkpoint(planned, "sensors")
    old = run["checkpoint"]["token"]
    backend.observations["camera"]["invocation_id"] = "changed"
    await planned.poll()
    current = planned.snapshot()["plan"]["checkpoint"]
    assert current["token"] != old and "changed" in current["notice"]
    with pytest.raises(ValueError, match="changed"):
        await planned.plans.continue_run(run["id"], old)
    assert ("detector", "start") not in backend.calls
    await proceed(planned, "sensors")
    await checkpoint(planned, "perception")
    planned.request_cancel()
    await planned.wait()


async def test_manager_loss_during_checkpoint_fails_without_resume(planned, backend):
    planned.plans.start(planned.plans.preview("startup")["id"])
    await checkpoint(planned, "sensors")
    backend.lost.set()
    result = await asyncio.wait_for(planned.wait(), 2)
    assert result["status"] == "failed" and "unknown" in result["error"]
    assert planned.plans.active is None and ("detector", "start") not in backend.calls
    backend.lost.clear()
    assert planned.snapshot()["plan"] is None


async def test_unknown_observer_is_not_a_success_source_or_ready_claim(planned, backend):
    async def unavailable(work):
        raise OSError("observer unavailable")

    backend.observe = unavailable
    planned.plans.start(planned.plans.preview("startup")["id"])
    run = await checkpoint(planned, "sensors")
    assert run["checkpoint"]["unknown"]
    assert all(v["state"] == "unknown" for v in run["checkpoint"]["observations"].values())
    assert all(
        row["status"] == "completed" for row in planned.run["progress"]
    )  # Adapter results, not cache.
    planned.request_cancel()
    await planned.wait()


async def test_core_restart_never_recovers_preview_run_or_confirmation(planned, backend):
    preview = planned.plans.preview("startup")
    planned.plans.start(preview["id"])
    await checkpoint(planned, "sensors")
    planned.task.cancel()
    await asyncio.gather(planned.task, return_exceptions=True)
    calls = list(backend.calls)
    recovered = Engine(planned.config, backend)
    await recovered.poll()
    assert recovered.snapshot()["plan"] is None and recovered.run["status"] == ""
    assert not recovered.plans.previews and backend.calls == calls
    with pytest.raises(ValueError, match="Unknown"):
        recovered.plans.start(preview["id"])


def test_no_checks_means_no_checkpoint_and_no_final_approval(config):
    steps = steps_for(config, "startup")
    assert [s["action"] for s in steps] == ["start", "start", "start"]
    assert all(s["kind"] == "operation" for s in steps)


def test_observer_is_independent_and_channels_recover_separately():
    config = Config(
        dict(
            workloads={
                "robot": dict(
                    backend="systemd",
                    unit="robot.service",
                    observer={"docker": {"container": "robot"}},
                )
            }
        )
    )
    backend = Fake()
    observer = Observer(config, backend)
    assert (
        not hasattr(observer, "run")
        and not hasattr(observer, "plans")
        and not hasattr(observer, "operator")
    )

    def facts(state, down=False):
        return observation(
            state=state,
            error="bus down" if state == "unknown" else None,
            internal=observation(state="unknown" if down else "active"),
            observer_error="docker down" if down else None,
        )

    for state, down in [
        ("active", False),
        ("unknown", False),
        ("unknown", True),
        ("unknown", False),
        ("active", False),
    ]:
        observer.update({"robot": facts(state, down)})
    kinds = [e["kind"] for e in observer.events]
    assert kinds == [
        "observation_lost",
        "observer_lost",
        "observer_restored",
        "observation_restored",
    ]
    assert not backend.calls


async def test_empty_checks_execute_without_implicit_approval(config, backend):
    engine = Engine(config, backend)
    preview = engine.plans.preview("startup")
    engine.plans.start(preview["id"])
    assert (await engine.wait())["status"] == "completed"
    assert engine.plans.active is None
    assert backend.calls == [
        ("camera", "start"),
        ("imu", "start"),
        ("detector", "start"),
        ("planner", "start"),
    ]


async def test_unknown_observation_does_not_convert_failed_job_to_success(planned, backend):
    backend.fail.add("camera")
    backend.observe = lambda work: asyncio.sleep(0, result=observation(state="active"))
    planned.plans.start(planned.plans.preview("startup")["id"])
    assert (await planned.wait())["status"] == "failed"
    assert planned.snapshot()["plan"] is None


def test_bad_templates_rejected(config):
    raw = config.json()
    for definition in (
        {"kind": "loop"},
        {"kind": "restart_from", "from_stage": "missing"},
        {"kind": "startup", "from_stage": "sensors"},
    ):
        raw["plan_templates"] = {"bad": definition}
        with pytest.raises(ValueError):
            Config(raw)


async def test_unrelated_change_during_dispatched_job_preserves_job_evidence(planned, backend):
    release = backend.block["camera"] = asyncio.Event()
    planned.plans.start(planned.plans.preview("startup")["id"])
    await until(lambda: len(backend.calls) == 2)
    for event in (
        dict(kind="operation_prepared", active_state="inactive"),
        dict(kind="invocation_changed", invocation_id="first"),
        dict(kind="invocation_changed", invocation_id="external"),
        dict(kind="job", correlation="requested", job_path="/job/1", job_result="done"),
    ):
        planned.on_backend_event(dict(workload="camera", scope="user", message="test", **event))
    release.set()
    result = await planned.wait()
    assert result["status"] == "failed" and "uncertain" in result["error"]
    camera = next(p for p in result["progress"] if p["workload"] == "camera")
    assert camera["status"] == "completed" and camera["jobs"] == [
        dict(path="/job/1", result="done")
    ]
    assert planned.plans.active is None and ("detector", "start") not in backend.calls


async def test_checkpoint_manager_event_invalidates_old_token(planned):
    planned.plans.start(planned.plans.preview("startup")["id"])
    run = await checkpoint(planned, "sensors")
    planned.on_backend_event(dict(kind="reloaded", message="units changed", scope="user"))
    assert planned.snapshot()["plan"]["checkpoint"]["token"] != run["checkpoint"]["token"]
    with pytest.raises(ValueError, match="changed"):
        await planned.plans.continue_run(run["id"], run["checkpoint"]["token"])
    planned.plans.cancel(run["id"])
    await planned.wait()


def test_manager_gap_immediately_hides_old_lifecycle_but_preserves_internal_observation():
    from devdash.model import readiness

    config = Config(
        dict(
            workloads={
                "robot": dict(
                    backend="systemd",
                    unit="robot.service",
                    observer={"docker": {"container": "robot"}},
                )
            }
        )
    )
    observer = Observer(config, Fake())
    facts = dict(
        robot=observation(
            state="active",
            health="healthy",
            readiness=readiness("ready", "docker health"),
            internal=observation(state="active"),
        )
    )
    observer.update(copy.deepcopy(facts))
    for kind in ("disconnected", "reconnected"):
        observer.on_backend_event(dict(kind=kind, message="bus changed", scope="user"))
        current = observer.current()["robot"]
        assert current["state"] == "unknown" and current["lifecycle_stale"]
        assert current["internal"]["state"] == "active" and current["health"] == "healthy"
    observer.update(copy.deepcopy(facts))
    assert observer.current()["robot"]["state"] == "active"
