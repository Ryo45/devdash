"""Actual Tk controls + IPC + systemd/Docker; no devdash readiness Gate or approval."""

import asyncio
import json
import os
import time

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.gui,
    pytest.mark.skipif(
        any(
            os.environ.get(key) != "1"
            for key in ("DEVDASH_GUI_TEST", "DEVDASH_SYSTEMD_TEST", "DEVDASH_DOCKER_TEST")
        ),
        reason="requires display, dedicated Docker fixture and real user manager",
    ),
]


@pytest.mark.parametrize("use_plan", [False, True])
async def test_gui_health_start_observation_next_stage(health_scenario, use_plan):
    from test_plan_health import enable_plan

    from devdash.gui import Window

    h = await health_scenario()
    if use_plan:
        enable_plan(h)
    h.monitor = asyncio.create_task(h.engine.monitor())
    window = Window(h.socket)
    callback_errors = []
    window.report_callback_exception = lambda *args: callback_errors.append(args)

    async def wait(predicate):
        deadline = time.monotonic() + 20
        while not predicate():
            assert time.monotonic() < deadline, (window.error.cget("text"), h.engine.run)
            window.update()
            await asyncio.sleep(0.01)

    def select(kind, key):
        window.tree.selection_set(json.dumps([kind, key]))
        window.tree.event_generate("<<TreeviewSelect>>")
        window.update()

    def click(button):
        assert not button.instate(["disabled"])
        button.invoke()
        window.update()

    try:
        await wait(lambda: window.connected)
        select("stage", "robot")
        if use_plan:
            click(window.plan_preview_button)
            await wait(lambda: hasattr(window, "plan_dialog"))
            assert h.calls == []
            click(window.plan_dialog.execute_button)
        else:
            click(window.buttons["start"])
        await h.health_wait()
        await wait(
            lambda: (
                window.snapshot["observations"].get("container", {}).get("state") == "activating"
            )
        )
        assert window.next_button.instate(["disabled"])
        assert h.jobs() == [] and len(h.calls) == 1
        select("workload", "container")
        assert "Lifecycle Controller: systemd" in window.summary.cget("text")
        assert "Internal Observer: Docker API" in window.summary.cget("text")
        h.release()
        await wait(
            lambda: (
                (
                    bool((window.snapshot.get("plan") or {}).get("checkpoint"))
                    if use_plan
                    else window.snapshot["run"]["status"] == "completed"
                )
                and window.snapshot["observations"].get("container", {}).get("health") == "healthy"
            )
        )
        obs = window.snapshot["observations"]["container"]
        assert obs["state"] == "active" and obs["readiness"]["state"] == "ready"
        assert any(child["kind"] == "process" for child in obs["children"])
        assert any(
            child["id"] == "worker:worker" and child["state"] == "RUNNING"
            for child in obs["children"]
        )
        assert window.snapshot["observations"]["next"]["state"] == "inactive"
        select("stage", "robot")
        if use_plan:
            assert "WAITING FOR RUN-LOCAL CONTINUE" in window.texts["Plan"].get("1.0", "end-1c")
            assert window.next_button.instate(["disabled"])
            click(window.plan_continue)
        else:
            assert "[completed]" in window.texts["Progress"].get("1.0", "end-1c")
            assert "Next: next" in window.next_button.cget("text")
            click(window.next_button)
        await wait(
            lambda: (
                window.snapshot["run"]["status"] == "completed"
                and window.snapshot["observations"].get("next", {}).get("state") == "active"
            )
        )
        assert h.calls == [(h.units["container"], "start"), (h.units["next"], "start")]
        assert h.jobs() == ["done"]
        assert window.snapshot["plan"] is None
        assert not callback_errors, callback_errors
        await h.record("gui-plan-continue" if use_plan else "gui-next")
    finally:
        window.close()
