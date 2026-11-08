"""Actual Stage/Next controls and offline Editor/Generator using the same models."""

import json
import os
import time

import pytest
from test_gui_lifecycle import CorePeer

pytestmark = [
    pytest.mark.gui,
    pytest.mark.skipif(os.environ.get("DEVDASH_GUI_TEST") != "1", reason="requires Tk display"),
]


def test_gui_stage_next_partial_restart_and_editor_generation(config, tmp_path):
    from devdash.generator import read_current
    from devdash.gui import Window
    from devdash.model import Config

    peer = CorePeer(config, tmp_path / "core.sock")
    window = Window(peer.socket)
    errors = []
    window.report_callback_exception = lambda *args: errors.append(args)

    def wait(predicate):
        deadline = time.monotonic() + 6
        while not predicate():
            assert time.monotonic() < deadline, window.error.cget("text")
            window.update()
            time.sleep(0.01)

    def select(kind, key):
        window.tree.selection_set(json.dumps([kind, key]))
        window.tree.event_generate("<<TreeviewSelect>>")
        window.update()

    def click(button):
        assert not button.instate(["disabled"])
        button.invoke()
        window.update()

    editor = None
    try:
        wait(lambda: window.connected)
        select("stage", "sensors")
        assert "detector" in window.next_button.cget("text")
        click(window.buttons["start"])
        wait(lambda: window.snapshot["run"]["status"] == "completed")
        assert set(peer.backend.calls) == {("camera", "start"), ("imu", "start")}
        assert "[completed]" in window.texts["Progress"].get("1.0", "end-1c")
        click(window.next_button)
        wait(
            lambda: (
                window.snapshot["run"]["target"] == "perception"
                and window.snapshot["run"]["status"] == "completed"
            )
        )
        assert peer.backend.calls[-1] == ("detector", "start")
        assert ("planner", "start") not in peer.backend.calls
        assert window.selection == ["stage", "perception"]
        assert "planner" in window.next_button.cget("text")
        click(window.next_button)
        wait(
            lambda: (
                window.snapshot["run"]["target"] == "control"
                and window.snapshot["run"]["status"] == "completed"
            )
        )
        assert peer.backend.calls[-1] == ("planner", "start")
        select("stage", "perception")
        click(window.buttons["restart"])
        wait(
            lambda: (
                peer.backend.calls[-1] == ("detector", "restart")
                and window.snapshot["run"]["action"] == "restart"
                and window.snapshot["run"]["status"] == "completed"
            )
        )
        select("workload", "detector")
        assert "Lifecycle Controller: systemd" in window.summary.cget("text")
        window.logs()
        wait(lambda: "fixture log" in window.texts["Logs"].get("1.0", "end-1c"))
        before = list(peer.backend.calls)
        editor = window.open_editor()

        def editor_click(text):
            def widgets(parent):
                for child in parent.winfo_children():
                    yield child
                    yield from widgets(child)

            button = next(
                widget
                for widget in widgets(editor)
                if widget.winfo_class() == "TButton" and widget.cget("text") == text
            )
            button.invoke()
            editor.update()

        editor.source.set(str(tmp_path / "edit.yaml"))
        editor.output.set(str(tmp_path / "generated"))
        # Create two independent stages and workloads through actual form widgets.
        for stage in ("first", "second"):
            editor.new_stage()
            editor.put(editor.stage_fields["id"], stage)
            editor.put(editor.stage_fields["name"], stage.title())
            editor_click("Apply Stage")
        for key, stage in (("one", "first"), ("two", "second")):
            editor.new_workload()
            for field, value in dict(id=key, unit=key + ".service", stage=stage).items():
                editor.put(editor.work_fields[field], value)
            editor_click("Apply Workload")
        editor.dependent.set("two")
        editor.prerequisites.selection_set("one")
        editor_click("Apply explicit dependencies")
        assert editor.config.dependencies == {"two": ["one"]}
        editor_click("Preview")
        plan = editor.plan
        assert any("Requisite=one.service" in item["diff"] for item in plan["changes"])
        editor_click("Generate")
        name, files = read_current(tmp_path / "generated")
        assert Config.parse(files["devdash.yaml"]).stages[0]["id"] == "first"
        assert (tmp_path / "edit.yaml").exists() and name
        assert peer.backend.calls == before  # Editor save/generate issues no lifecycle requests.
        # Invalid reordering fails without changing the model.
        editor.stage_list.selection_set("second")
        editor_click("Up")
        assert editor.config.stages[0]["id"] == "first"
        assert "earlier Stage" in editor.message.cget("text")
        assert not errors, errors
    finally:
        if editor:
            editor.close()
        window.close()
        peer.close()
