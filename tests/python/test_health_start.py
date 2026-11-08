"""Real systemd Start job waits on Docker Health; devdash only sequences/approves."""

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


async def test_health_start_then_manual_continue_and_group_restart_stop(health_scenario):
    h = await health_scenario()
    await h.command("operate", target="robot", group=True, action="start")
    waiting = await h.health_wait()
    assert waiting["readiness"]["state"] == "not_ready"
    assert h.engine.run["waiting_gate"] is None and h.engine.busy
    assert h.jobs() == [] and (await h.observe("next"))["state"] == "inactive"
    # A competing devdash Stop/Restart is rejected, rather than cancelling the issued job.
    for action in ("stop", "restart"):
        with pytest.raises(RuntimeError, match="Another operation"):
            await h.command("operate", target="robot", group=True, action=action)
    assert len(h.calls) == 1
    h.release()

    async def approval_wait():
        return h.engine.run["waiting_gate"] == "operator"

    await eventually(approval_wait)
    value = await h.observe()
    assert value["state"] == "active" and value["health"] == "healthy"
    assert any(c["kind"] == "process" for c in value["children"])
    assert any(c["id"] == "worker:worker" and c["state"] == "RUNNING" for c in value["children"])
    assert h.jobs() == ["done"] and (await h.observe("next"))["state"] == "inactive"
    assert h.engine.run["progress"][0]["status"] == "completed"
    await h.command("approve", gate="operator")
    assert (await h.done())["status"] == "completed"
    assert (await h.observe("next"))["state"] == "active"
    initial = value["internal"]["invocation_id"]
    h.calls.clear()
    await h.command("operate", target="robot", group=True, action="restart")
    assert (await h.done())["status"] == "completed"
    assert h.calls == [
        (h.units["next"], "stop"),
        (h.units["container"], "stop"),
        (h.units["container"], "start"),
        (h.units["next"], "start"),
    ]
    assert "operator" in h.engine.approved
    assert (await h.observe())["internal"]["invocation_id"] != initial
    await h.record("success-and-restart")
    h.calls.clear()
    await h.command("operate", target="robot", group=True, action="stop")
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
    await h.command("operate", target="robot", group=True, action="start")
    if failure == "container-exit":
        await h.health_wait()
        await h.command_process(h.docker, "kill", h.name)
    run = await h.done()
    assert run["status"] == "failed" and f"job result {job_result}" in run["error"]
    assert h.jobs() == [job_result]
    assert run["waiting_gate"] is None and "operator" not in h.engine.approved
    assert [p["status"] for p in run["progress"]] == ["failed", "skipped", "skipped"]
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
    await h.command("operate", target="robot", group=True, action="start")
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
        "skipped",
        "skipped",
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
    await h.command("operate", target="robot", group=True, action="start")
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

        async def gate_or_failure():
            return h.engine.run["waiting_gate"] == "operator" or h.engine.run["status"] == "failed"

        await eventually(gate_or_failure)
        await h.record("external-restart")
        assert (await h.observe("next"))["state"] == "inactive"
        if h.engine.run["waiting_gate"]:
            assert h.jobs() == ["done"]
            await h.command("cancel")
            assert (await h.done())["status"] == "cancelled"
        else:
            assert "job result canceled" in h.engine.run["error"]
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
    assert "operator" not in h.engine.approved
    assert (await h.observe("next"))["state"] == "inactive"
