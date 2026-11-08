"""Actual Tk controls + IPC + systemd/Docker; no devdash readiness Gate."""

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


async def test_gui_health_start_observation_continue_next_service(health_scenario):
    from devdash.gui import Window

    h = await health_scenario()
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
        select("group", "robot")
        click(window.buttons["start"])
        await h.health_wait()
        await wait(
            lambda: (
                window.snapshot["observations"].get("container", {}).get("state") == "activating"
            )
        )
        assert window.snapshot["run"]["waiting_gate"] is None
        assert window.approve.instate(["disabled"])
        assert h.jobs() == [] and len(h.calls) == 1
        select("workload", "container")
        assert "Lifecycle Controller: systemd" in window.summary.cget("text")
        assert "Internal Observer: Docker API" in window.summary.cget("text")
        h.release()
        await wait(
            lambda: (
                window.snapshot["run"]["waiting_gate"] == "operator"
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
        select("group", "robot")
        assert "WAITING FOR CONTINUE" in window.texts["Progress"].get("1.0", "end-1c")
        assert "[NEXT] start · workload · next" in window.texts["Progress"].get("1.0", "end-1c")
        assert "Developer Continue: operator" in window.run.cget("text")
        click(window.approve)
        await wait(
            lambda: (
                window.snapshot["run"]["status"] == "completed"
                and window.snapshot["observations"].get("next", {}).get("state") == "active"
            )
        )
        assert h.calls == [(h.units["container"], "start"), (h.units["next"], "start")]
        assert h.jobs() == ["done"]
        assert not callback_errors, callback_errors
        await h.record("gui-continue")
    finally:
        window.close()
