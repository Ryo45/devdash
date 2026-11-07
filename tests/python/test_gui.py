"""Real Qt clicks through IPC to an independent Core thread, with Fake Backend."""

import asyncio
import os
import threading
import time

import pytest

pytestmark = [
    pytest.mark.gui,
    pytest.mark.skipif(
        os.environ.get("DEVDASH_GUI_TEST") != "1",
        reason="set DEVDASH_GUI_TEST=1 and QT_QPA_PLATFORM=offscreen",
    ),
]


def test_gui_group_approve_partial_restart_cancel_and_logs(config, tmp_path):
    pytest.importorskip("PySide6")
    from conftest import Fake
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from devdash.engine import Engine
    from devdash.gui import Window, apply_theme
    from devdash.ipc import Server

    socket = tmp_path / "core.sock"
    state = {}
    ready = threading.Event()

    async def core():
        backend = Fake()
        engine = Engine(config, backend)
        server = Server(engine, tmp_path / "config.yaml")
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
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    window = Window(socket)
    window.show()

    def wait(predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate():
            assert time.monotonic() < deadline, window.status.text()
            QTest.qWait(20)
            app.processEvents()

    def select(identity):
        def find(item):
            if item.data(0, Qt.UserRole) == identity:
                return item
            for index in range(item.childCount()):
                if found := find(item.child(index)):
                    return found
            return None

        for index in range(window.tree.topLevelItemCount()):
            if item := find(window.tree.topLevelItem(index)):
                window.tree.setCurrentItem(item)
                return
        raise AssertionError(identity)

    def click(button):
        assert button.isEnabled()
        QTest.mouseClick(button, Qt.LeftButton)

    try:
        wait(lambda: window.snapshot is not None)
        select(["group", "robot"])
        click(window.buttons["start"])
        wait(lambda: window.snapshot["run"]["waiting_gate"] == "ready")
        assert len(state["backend"].calls) == 2
        assert not window.buttons["restart"].isEnabled()
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
        wait(lambda: "fixture log" in window.texts["Logs"].toPlainText())
        assert window.buttons["start"].isEnabled()
        click(window.buttons["start"])
        wait(
            lambda: (
                window.snapshot["run"]["target"] == "detector"
                and window.snapshot["run"]["status"] == "completed"
            )
        )
        assert state["backend"].calls[-1] == ("detector", "start")
        assert "ready" not in window.snapshot["approved"]
        screenshot = os.environ.get("DEVDASH_GUI_SCREENSHOT")
        if screenshot:
            assert window.grab().save(screenshot)
    finally:
        window.close()
        state["loop"].call_soon_threadsafe(state["stop"].set)
        thread.join(5)
        assert not thread.is_alive()
