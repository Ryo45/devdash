"""Plan state stays separate from real systemd jobs and Docker observations."""

from health_scenario import eventually
from test_health_start import pytestmark as pytestmark

from devdash.model import Config


def enable_plan(h):
    raw = h.config.json()
    raw["stages"][0]["checks"] = ["Check actual images before next service"]
    h.config = h.engine.config = h.backend.systemd.config = Config(raw)


async def at_checkpoint(h):
    async def read():
        assert h.engine.run["status"] != "failed", h.engine.run.get("error")
        value = h.engine.snapshot()["plan"]
        return value if value and value["checkpoint"] else None

    return await eventually(read)


async def start_plan(h, kind="startup", **args):
    preview = await h.command("plan_preview", kind=kind, **args)
    assert h.engine.plans.active is None
    await h.command("plan_execute", preview_id=preview["id"])
    return h.engine.plans.active["id"]


async def test_real_health_job_checkpoint_restart_suffix_and_free_operation(health_scenario):
    h = await health_scenario()
    enable_plan(h)
    await start_plan(h)
    await h.health_wait()
    assert h.jobs() == [] and (await h.observe("next"))["state"] == "inactive"
    h.release()
    run = await at_checkpoint(h)
    fact = run["checkpoint"]["observations"]["container"]
    assert h.jobs() == ["done"]
    assert fact["state"] == "active" and fact["health"] == "healthy"
    assert any(c["id"] == "worker:worker" and c["state"] == "RUNNING" for c in fact["children"])
    assert (await h.observe("next"))["state"] == "inactive"
    await h.command("plan_continue", run_id=run["id"], token=run["checkpoint"]["token"])
    assert (await h.done())["status"] == "completed"
    assert h.engine.snapshot()["plan"] is None
    await h.record("plan-startup-checkpoint")
    h.calls.clear()
    await start_plan(h, "restart_from", from_stage="robot")
    run = await at_checkpoint(h)
    assert h.calls == [
        (h.units["next"], "stop"),
        (h.units["container"], "stop"),
        (h.units["container"], "start"),
    ]
    await h.command("plan_continue", run_id=run["id"], token=run["checkpoint"]["token"])
    assert (await h.done())["status"] == "completed"
    assert h.calls[-1] == (h.units["next"], "start")
    # An active Start is an idempotent manager job, never an implicit Restart.
    before = (await h.observe())["invocation_id"]
    await h.command("operate", target="container", kind="workload", action="start")
    assert (await h.done())["status"] == "completed"
    assert (await h.observe())["invocation_id"] == before


async def test_plan_abort_during_real_job_drains_and_skips_checkpoint_and_next(health_scenario):
    h = await health_scenario()
    enable_plan(h)
    run_id = await start_plan(h)
    await h.health_wait()
    jobs = set(h.backend.systemd.session("user").jobs)
    await h.command("plan_cancel", run_id=run_id)
    assert h.engine.run["status"] == "cancelling" and h.engine.busy
    assert set(h.backend.systemd.session("user").jobs) == jobs
    h.release()
    result = await h.done()
    assert result["status"] == "cancelled" and h.jobs() == ["done"]
    assert result["progress"][0]["status"] == "completed"
    assert (await h.observe())["state"] == "active"
    assert (await h.observe("next"))["state"] == "inactive"
    assert h.engine.plans.active is None and not h.engine.busy
    await h.command("operate", target="next", kind="workload", action="start")
    assert (await h.done())["status"] == "completed"
    await h.record("plan-abort-drains-job")


async def test_external_restart_merged_job_does_not_advance_plan(health_scenario):
    h = await health_scenario()
    enable_plan(h)
    await start_plan(h)
    first = await h.health_wait()
    await h.command_process("systemctl", "--user", "--no-block", "restart", h.units["container"])

    async def changed():
        value = await h.observe()
        return (
            value
            if value["sub_state"] == "start-post"
            and value["invocation_id"] != first["invocation_id"]
            else None
        )

    await eventually(changed)
    h.release()
    result = await h.done()
    assert result["status"] == "failed"
    assert "uncertain" in result["error"] or "canceled" in result["error"]
    assert (await h.observe("next"))["state"] == "inactive"
    assert h.engine.plans.active is None
    assert h.jobs() in (["done"], ["canceled"])
    await h.record("plan-external-restart-uncertain")


async def test_external_restart_at_checkpoint_requires_new_review(health_scenario):
    import pytest

    h = await health_scenario()
    enable_plan(h)
    h.release()
    await start_plan(h)
    old = await at_checkpoint(h)
    await h.command_process("systemctl", "--user", "restart", h.units["container"])
    await h.engine.poll()
    fresh = h.engine.snapshot()["plan"]
    assert fresh["checkpoint"]["token"] != old["checkpoint"]["token"]
    assert (
        fresh["checkpoint"]["observations"]["container"]["invocation_id"]
        != old["checkpoint"]["observations"]["container"]["invocation_id"]
    )
    with pytest.raises(RuntimeError, match="changed"):
        await h.command("plan_continue", run_id=old["id"], token=old["checkpoint"]["token"])
    assert (await h.observe("next"))["state"] == "inactive"
    await h.command("plan_continue", run_id=fresh["id"], token=fresh["checkpoint"]["token"])
    assert (await h.done())["status"] == "completed"
    assert h.engine.plans.active is None
