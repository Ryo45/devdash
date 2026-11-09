"""The real Tk controls use preview, Run-local Continue and independent operations."""

import json
import time

from test_gui_lifecycle import CorePeer


def test_gui_plan_preview_checkpoints_repeat_abort_and_normal_operation(config, tmp_path):
    from devdash.gui import Window
    from devdash.model import Config

    raw = config.json()
    raw["stages"][0]["checks"] = ["Check camera images and IMU"]
    raw["stages"][1]["checks"] = ["Check perception output"]
    raw["plan_templates"] = {"boot": {"kind": "startup"}}
    peer = CorePeer(Config(raw), tmp_path / "core.sock")
    window = Window(peer.socket)
    errors = []
    window.report_callback_exception = lambda *args: errors.append(args)

    def wait(predicate):
        deadline = time.monotonic() + 8
        while not predicate():
            assert time.monotonic() < deadline, window.error.cget("text")
            window.update()
            time.sleep(0.01)

    def checkpoint(stage):
        return ((window.snapshot.get("plan") or {}).get("checkpoint") or {}).get("stage") == stage

    def click(button):
        assert not button.instate(["disabled"])
        button.invoke()
        window.update()

    def preview():
        window.plan_kind.set("template:boot")
        click(window.plan_preview_button)
        wait(lambda: hasattr(window, "plan_dialog") and window.plan_dialog.winfo_exists())

    try:
        wait(lambda: window.connected)
        preview()
        assert not peer.backend.calls and window.snapshot["plan"] is None
        click(window.plan_dialog.execute_button)
        wait(lambda: checkpoint("sensors"))
        first_id = window.snapshot["plan"]["id"]
        assert peer.backend.calls == [("camera", "start"), ("imu", "start")]
        assert "Check camera" in window.texts["Plan"].get("1.0", "end-1c")
        assert "Remaining" in window.texts["Plan"].get("1.0", "end-1c")
        click(window.plan_continue)
        wait(lambda: checkpoint("perception"))
        assert peer.backend.calls[-1] == ("detector", "start")
        click(window.plan_continue)
        wait(lambda: window.snapshot["run"]["status"] == "completed")
        assert window.snapshot["plan"] is None
        assert peer.backend.calls[-1] == ("planner", "start")
        preview()
        click(window.plan_dialog.execute_button)
        wait(lambda: checkpoint("sensors"))
        assert window.snapshot["plan"]["id"] != first_id
        # GUI closing/reopening owns neither the Plan nor its Checkpoint.
        window.close()
        assert peer.engine.busy and peer.engine.plans.active
        window = Window(peer.socket)
        window.report_callback_exception = lambda *args: errors.append(args)
        wait(lambda: window.connected and checkpoint("sensors"))
        click(window.plan_abort)
        wait(lambda: window.snapshot["run"]["status"] == "cancelled")
        assert window.snapshot["plan"] is None
        window.tree.selection_set(json.dumps(["workload", "detector"]))
        window.tree.event_generate("<<TreeviewSelect>>")
        window.update()
        click(window.buttons["restart"])
        wait(
            lambda: (
                window.snapshot["run"]["kind"] == "workload"
                and window.snapshot["run"]["status"] == "completed"
            )
        )
        assert peer.backend.calls[-1] == ("detector", "restart")
        assert not errors, errors
    finally:
        window.close()
        peer.close()
