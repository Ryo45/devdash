"""Real Tkinter controls through IPC to an independent Core thread, with Fake Backend."""

import asyncio
import os
import threading
import time

import pytest

pytestmark = [
    pytest.mark.gui,
    pytest.mark.skipif(
        os.environ.get("DEVDASH_GUI_TEST") != "1",
        reason="set DEVDASH_GUI_TEST=1 and run with a display or xvfb-run",
    ),
]


def test_gui_group_approve_partial_restart_cancel_and_logs(config, tmp_path):
    import json

    from conftest import Fake

    from devdash.engine import Engine
    from devdash.gui import Window
    from devdash.ipc import Server

    socket = tmp_path / "core.sock"
    state = {}
    ready = threading.Event()

    async def core():
        backend = Fake()
        engine = Engine(config, backend)
        import yaml

        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(config.json()))
        backend.systemd = backend  # Import updates the adapter's config reference.
        backend.config = config
        server = Server(engine, path)
        listener = await asyncio.start_unix_server(server.handle, path=str(socket))
        state.update(
            loop=asyncio.get_running_loop(), engine=engine, backend=backend, stop=asyncio.Event()
        )
        monitor = asyncio.create_task(engine.monitor())
        ready.set()
        try:
            await state["stop"].wait()
        finally:
            listener.close()
            await listener.wait_closed()
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)

    thread = threading.Thread(target=lambda: asyncio.run(core()), daemon=True)
    thread.start()
    assert ready.wait(5)
    window = Window(socket)
    callback_errors = []
    window.report_callback_exception = lambda *args: callback_errors.append(args)

    def wait(predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate():
            assert time.monotonic() < deadline, window.error.cget("text")
            window.update()
            time.sleep(0.01)

    def select(identity):
        iid = json.dumps(identity, ensure_ascii=False)
        assert window.tree.exists(iid), identity
        window.tree.selection_set(iid)
        window.tree.focus(iid)
        window.tree.event_generate("<<TreeviewSelect>>")
        window.update()

    def click(button):
        assert not button.instate(["disabled"])
        button.invoke()
        window.update()

    try:
        wait(lambda: window.snapshot is not None)
        select(["group", "robot"])
        click(window.buttons["start"])
        wait(lambda: window.snapshot["run"]["waiting_gate"] == "ready")
        assert len(state["backend"].calls) == 2
        assert window.buttons["restart"].instate(["disabled"])
        click(window.approve)
        wait(lambda: window.snapshot["run"]["status"] == "completed")
        assert state["backend"].calls[-2:] == [("detector", "start"), ("planner", "start")]
        select(["group", "perception"])
        before = len(state["backend"].calls)
        click(window.buttons["restart"])
        wait(
            lambda: (
                len(state["backend"].calls) == before + 2
                and window.snapshot["run"]["target"] == "perception"
                and window.snapshot["run"]["status"] == "completed"
            )
        )
        assert state["backend"].calls[-2:] == [("detector", "stop"), ("detector", "start")]
        select(["group", "robot"])
        click(window.buttons["reinitialize"])
        wait(lambda: window.snapshot["run"]["waiting_gate"] == "ready")
        click(window.cancel)
        wait(lambda: window.snapshot["run"]["status"] == "cancelled")
        assert any(step["status"] == "skipped" for step in window.snapshot["run"]["progress"])
        select(["workload", "detector"])
        window.logs()
        wait(lambda: "fixture log" in window.texts["Logs"].get("1.0", "end-1c"))
        assert not window.buttons["start"].instate(["disabled"])
        click(window.buttons["start"])
        wait(
            lambda: (
                window.snapshot["run"]["target"] == "detector"
                and window.snapshot["run"]["status"] == "completed"
            )
        )
        assert state["backend"].calls[-1] == ("detector", "start")
        assert "ready" not in window.snapshot["approved"]
        # Actual Scan/Import controls, including scope-change selection reset.
        dialog = window.scan_dialog()
        click(dialog.scan_button)
        wait(lambda: bool(dialog.units.get_children()))
        dialog.units.selection_set("importable.service")
        dialog.scope.set("system")
        dialog.scope.event_generate("<<ComboboxSelected>>")
        window.update()
        assert not dialog.units.get_children()
        click(dialog.scan_button)
        wait(lambda: bool(dialog.units.get_children()))
        dialog.units.selection_set("importable.service")
        click(dialog.import_button)
        wait(lambda: "Imported:" in dialog.message.cget("text"))
        wait(lambda: "importable" in window.snapshot["config"]["workloads"])
        assert window.snapshot["config"]["workloads"]["importable"]["scope"] == "system"
        assert (tmp_path / "config.yaml.bak").exists()
        dialog.close()
        assert not callback_errors, callback_errors
        if screenshot := os.environ.get("DEVDASH_GUI_SCREENSHOT"):
            from PIL import ImageGrab

            window.update()
            x, y = window.winfo_rootx(), window.winfo_rooty()
            ImageGrab.grab(bbox=(x, y, x + window.winfo_width(), y + window.winfo_height())).save(
                screenshot
            )
    finally:
        window.close()
        state["loop"].call_soon_threadsafe(state["stop"].set)
        thread.join(5)
        assert not thread.is_alive()
