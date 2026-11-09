"""Real health startup failure, then a new state-based Plan, with Camera left running."""

from health_scenario import eventually
from test_health_start import pytestmark as pytestmark
from test_plan_health import at_checkpoint

from devdash.model import Config


async def test_real_failure_then_new_plan_skips_running_camera(health_scenario):
    h = await health_scenario()
    raw = h.config.json()
    for key in ("detector", "planner"):
        unit = f"{h.name}-{key}.service"
        h.units[key] = unit
        path = h.runtime / unit
        path.write_text("[Service]\nType=exec\nExecStart=/bin/sleep 3600\n")
        h.files.append(path)
        raw["workloads"][key] = dict(backend="systemd", unit=unit)
    raw["stages"] = [
        dict(id="sensors", workloads=["next", "container"], checks=["Check Camera/IMU"]),
        dict(id="perception", workloads=["detector"], checks=["Check recognition"]),
        dict(id="control", workloads=["planner"]),
    ]
    h.config = h.engine.config = h.backend.systemd.config = Config(raw)
    await h.backend.systemd.session("user").call("Reload")
    await h.command("operate", target="next", kind="workload", action="start")
    assert (await h.done())["status"] == "completed"
    camera = await h.observe("next")
    h.calls.clear()
    preview = await h.command("plan_preview", kind="start_missing")
    assert preview["steps"][0]["preview"]["next"]["action"] == "skip"
    await h.command("plan_execute", preview_id=preview["id"])
    first_id = h.engine.plans.active["id"]
    await h.health_wait()
    await h.command_process(h.docker, "kill", h.name)
    failed = await h.done()
    assert failed["status"] == "failed" and h.engine.plans.active is None
    assert h.calls == [(h.units["container"], "start")]
    assert (await h.observe("container"))["state"] == "failed"
    assert (await h.observe("next"))["pid"] == camera["pid"]
    # Repair only the dummy Healthcheck input. No old Run/approval/reset-failed.
    h.release()
    preview = await h.command("plan_preview", kind="start_missing")
    assert preview["steps"][0]["preview"]["container"]["action"] == "start"
    await h.command("plan_execute", preview_id=preview["id"])
    run = await at_checkpoint(h)
    assert run["id"] != first_id and run["checkpoint"]["stage"] == "sensors"
    assert (await h.observe("detector"))["state"] == "inactive"
    await h.command("plan_continue", run_id=run["id"], token=run["checkpoint"]["token"])

    async def perception():
        value = h.engine.snapshot()["plan"]
        return value if value and (value["checkpoint"] or {}).get("stage") == "perception" else None

    run = await eventually(perception)
    await h.command("plan_continue", run_id=run["id"], token=run["checkpoint"]["token"])
    assert (await h.done())["status"] == "completed"
    assert h.calls == [
        (h.units["container"], "start"),
        (h.units["container"], "start"),
        (h.units["detector"], "start"),
        (h.units["planner"], "start"),
    ]
    assert (await h.observe("next"))["invocation_id"] == camera["invocation_id"]
    # A second all-active Run still waits, issuing no requests.
    h.calls.clear()
    preview = await h.command("plan_preview", kind="start_missing")
    await h.command("plan_execute", preview_id=preview["id"])
    run = await at_checkpoint(h)
    assert h.calls == []
    await h.command("plan_cancel", run_id=run["id"])
    assert (await h.done())["status"] == "cancelled"
