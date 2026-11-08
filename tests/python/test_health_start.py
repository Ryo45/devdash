"""Real systemd Start job waits on Docker Health; devdash explicitly requests Stages."""

import os

import pytest
from health_scenario import eventually

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("DEVDASH_DOCKER_TEST") != "1"
        or os.environ.get("DEVDASH_SYSTEMD_TEST") != "1",
        reason="requires dedicated Docker fixture and real user manager",
    ),
]


async def test_health_start_then_explicit_next_and_stage_restart_stop(health_scenario):
    h = await health_scenario()
    await h.command("operate", target="robot", kind="stage", action="start")
    waiting = await h.health_wait()
    assert waiting["readiness"]["state"] == "not_ready"
    assert h.engine.busy
    assert h.jobs() == [] and (await h.observe("next"))["state"] == "inactive"
    # A competing devdash Stop/Restart is rejected, rather than cancelling the issued job.
    for action in ("stop", "restart"):
        with pytest.raises(RuntimeError, match="Another operation"):
            await h.command("operate", target="robot", kind="stage", action=action)
    assert len(h.calls) == 1
    h.release()

    assert (await h.done())["status"] == "completed"
    value = await h.observe()
    assert value["state"] == "active" and value["health"] == "healthy"
    assert any(c["kind"] == "process" for c in value["children"])
    assert any(c["id"] == "worker:worker" and c["state"] == "RUNNING" for c in value["children"])
    assert h.jobs() == ["done"] and (await h.observe("next"))["state"] == "inactive"
    assert h.engine.run["progress"][0]["status"] == "completed"
    await h.command("next", after="robot")
    assert (await h.done())["status"] == "completed"
    assert (await h.observe("next"))["state"] == "active"
    initial = value["internal"]["invocation_id"]
    h.calls.clear()
    await h.command("operate", target="robot", kind="stage", action="restart")
    assert (await h.done())["status"] == "completed"
    assert h.calls == [(h.units["container"], "restart")]
    assert (await h.observe())["internal"]["invocation_id"] != initial
    await h.record("success-and-restart")
    h.calls.clear()
    await h.command("operate", target="next", kind="stage", action="stop")
    assert (await h.done())["status"] == "completed"
    await h.command("operate", target="robot", kind="stage", action="stop")
    assert (await h.done())["status"] == "completed"
    assert h.calls == [(h.units["next"], "stop"), (h.units["container"], "stop")]
    value = await h.observe()
    assert value["state"] == "inactive" and value["internal"]["state"] == "inactive"


@pytest.mark.parametrize(
    "failure,options,job_result,service_result",
    [
        ("unhealthy", {"start_period": "0s"}, "failed", "exit-code"),
        ("probe-deadline", {"health_timeout": 2}, "failed", "exit-code"),
        ("systemd-deadline", {"health_timeout": 30, "unit_timeout": 3}, "failed", "timeout"),
        ("container-exit", {}, "failed", "exit-code"),
    ],
)
async def test_health_start_failure_never_advances_and_unit_cleans_up(
    health_scenario,
    failure,
    options,
    job_result,
    service_result,
):
    h = await health_scenario(**options)
    await h.command("operate", target="robot", kind="stage", action="start")
    if failure == "container-exit":
        await h.health_wait()
        await h.command_process(h.docker, "kill", h.name)
    run = await h.done()
    assert run["status"] == "failed" and f"job result {job_result}" in run["error"]
    assert h.jobs() == [job_result]
    assert [p["status"] for p in run["progress"]] == ["failed"]
    assert h.calls == [(h.units["container"], "start")]  # No devdash rollback/cleanup.
    assert (await h.observe("next"))["state"] == "inactive"
    evidence = await h.record(failure)
    assert evidence["service_result"] == service_result
    assert evidence["observed"]["state"] == "failed"
    assert evidence["observed"]["internal"]["state"] in ("inactive", "failed")


@pytest.mark.parametrize("fail_after_cancel", [False, True])
async def test_cancel_drains_issued_health_wait_and_never_starts_next(
    health_scenario, fail_after_cancel
):
    h = await health_scenario()
    await h.command("operate", target="robot", kind="stage", action="start")
    await h.health_wait()
    jobs = set(h.backend.systemd.session("user").jobs)
    reply = await h.command("cancel")
    assert reply["run"]["status"] == "cancelling" and h.engine.busy
    assert set(h.backend.systemd.session("user").jobs) == jobs and h.jobs() == []
    if fail_after_cancel:
        await h.command_process(h.docker, "kill", h.name)
    else:
        h.release()
    run = await h.done()
    assert run["status"] == ("failed" if fail_after_cancel else "cancelled")
    assert h.jobs() == (["failed"] if fail_after_cancel else ["done"])
    assert [p["status"] for p in run["progress"]] == [
        "failed" if fail_after_cancel else "completed",
    ]
    assert h.calls == [(h.units["container"], "start")]
    assert (await h.observe("next"))["state"] == "inactive"
    observed = await h.observe()
    if not fail_after_cancel:
        assert (
            observed["state"] == "active" and observed["health"] == "healthy"
        )  # Cancel did not roll back.
    await h.record("cancel-then-failure" if fail_after_cancel else "cancel-then-healthy")


@pytest.mark.parametrize("external", ["stop", "restart"])
async def test_external_stop_or_restart_during_pending_start(health_scenario, external):
    h = await health_scenario()
    await h.command("operate", target="robot", kind="stage", action="start")
    first = await h.health_wait()
    pending = set(h.backend.systemd.session("user").jobs)
    await h.command_process("systemctl", "--user", "--no-block", external, h.units["container"])
    await h.record("external-request-" + external)
    if external == "restart":

        async def second_wait():
            value = await h.observe()
            return (
                value
                if value["sub_state"] == "start-post"
                and value.get("internal", {}).get("invocation_id")
                != first["internal"]["invocation_id"]
                else None
            )

        await eventually(second_wait)
        # systemd can merge Restart with a pending Start rather than remove its job.
        same_job = bool(pending & set(h.backend.systemd.session("user").jobs))
        print("Restart kept original tracked job:", same_job, flush=True)
        h.release()

        run = await h.done()
        await h.record("external-restart")
        assert (await h.observe("next"))["state"] == "inactive"
        assert run["status"] in ("completed", "failed")
        assert h.jobs() == (["done"] if run["status"] == "completed" else ["canceled"])
    else:
        run = await h.done()
        assert run["status"] == "failed" and "job result canceled" in run["error"]

        async def settled():
            value = await h.observe()
            return (
                value
                if value["state"] in ("inactive", "failed")
                and value["internal"]["state"] in ("inactive", "failed")
                else None
            )

        await eventually(settled)
        await h.record("external-stop")
    assert h.calls == [(h.units["container"], "start")]
    assert (await h.observe("next"))["state"] == "inactive"


async def test_generated_requisite_after_refuses_later_stage_without_pulling_prerequisite(
    health_scenario, tmp_path
):
    """Explicit test-only application of generated drop-in; generator never reloads host."""
    import shutil

    from devdash.generator import generate
    from devdash.model import Config

    h = await health_scenario()
    raw = h.config.json()
    raw["dependencies"] = {"next": ["container"]}
    candidate = Config(raw)
    plan = generate(candidate, tmp_path / "generated")
    relative = f"systemd/user/{h.units['next']}.d/60-devdash-stage-dependencies.conf"
    assert relative in plan["files"]
    target = h.runtime / f"{h.units['next']}.d" / "60-devdash-stage-dependencies.conf"
    target.parent.mkdir()
    try:
        # Application is explicit and confined to our unique dummy unit.
        shutil.copyfile(tmp_path / "generated/current" / relative, target)
        await h.backend.systemd.session("user").call("Reload")
        h.config = h.engine.config = h.backend.systemd.config = candidate
        report = await h.engine.inspect("next", "start")
        assert not any(d["level"] == "error" for d in report["diagnostics"]), report
        await h.command("operate", target="next", kind="stage", action="start")
        failed = await h.done()
        assert failed["status"] == "failed" and "dependency" in failed["error"].lower()
        assert (await h.observe())["state"] == "inactive"  # Requisite did not pull it in.
        assert (await h.observe("next"))["state"] == "inactive"
        h.release()
        await h.command("operate", target="robot", kind="stage", action="start")
        assert (await h.done())["status"] == "completed"
        assert (await h.observe("next"))["state"] == "inactive"
        await h.command("next", after="robot")
        assert (await h.done())["status"] == "completed"
        assert (await h.observe("next"))["state"] == "active"
        report = await h.engine.inspect("robot", "stop")
        assert any(d["code"] == "outside_stop" for d in report["diagnostics"])
        await h.record("generated-requisite-after")
        # Requisite is not an isolation boundary: explicit prerequisite Stop can propagate.
        await h.command("operate", target="robot", kind="stage", action="stop")
        assert (await h.done())["status"] == "completed"
        assert (await h.observe("next"))["state"] == "inactive"
    finally:
        # Stop dependents before removing their definition; keep fixture cleanup reproducible.
        await h.command_process("systemctl", "--user", "stop", h.units["next"], check=False)
        target.unlink(missing_ok=True)
        target.parent.rmdir()
        await h.backend.systemd.session("user").call("Reload")
