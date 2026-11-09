"""Desktop client reconnects; closing/reopening it never owns Core lifetime."""

import asyncio
import json
import os
import threading
import time

import pytest

pytestmark = [
    pytest.mark.gui,
    pytest.mark.skipif(
        os.environ.get("DEVDASH_GUI_TEST") != "1", reason="set DEVDASH_GUI_TEST=1 with a display"
    ),
]


class CorePeer:
    def __init__(self, config, socket):
        self.config, self.socket = config, socket
        self.ready = threading.Event()
        self.errors = []
        self.listener = None
        self.thread = threading.Thread(target=self.work, daemon=True)
        self.thread.start()
        assert self.ready.wait(5) and not self.errors, self.errors

    def work(self):
        try:
            asyncio.run(self.serve())
        except BaseException as error:
            self.errors.append(error)
            self.ready.set()

    async def serve(self):
        from conftest import Fake

        self.loop = asyncio.get_running_loop()
        self.backend = Fake()
        self.stop = asyncio.Event()
        await self.restart()
        self.ready.set()
        try:
            await self.stop.wait()
        finally:
            await self.disconnect()
            tasks = [task for task in (self.monitor, self.engine.task) if task and not task.done()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def run(self, operation):
        return asyncio.run_coroutine_threadsafe(operation, self.loop).result(timeout=5)

    async def disconnect(self):
        if self.listener:
            self.listener.close()
            await self.listener.wait_closed()
            self.listener = None
            clients = list(self.server.clients)
            for client in clients:
                client.cancel()
            await asyncio.gather(*clients, return_exceptions=True)

    async def connect(self):
        self.socket.unlink(missing_ok=True)
        self.listener = await asyncio.start_unix_server(self.server.handle, path=str(self.socket))

    async def restart(self):
        from devdash.engine import Engine
        from devdash.ipc import Server

        if self.listener:
            await self.disconnect()
        if hasattr(self, "monitor"):
            tasks = [task for task in (self.monitor, self.engine.task) if task and not task.done()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.engine = Engine(self.config, self.backend)
        self.server = Server(self.engine, self.socket.with_suffix(".yaml"))
        self.monitor = asyncio.create_task(self.engine.monitor())
        await self.connect()

    async def metadata(self, **values):
        self.engine.core.update(values)

    async def release(self):
        self.backend.block["camera"].set()
        return await self.engine.wait()

    def close(self):
        self.loop.call_soon_threadsafe(self.stop.set)
        self.thread.join(5)
        assert not self.thread.is_alive() and not self.errors, self.errors


def test_gui_reconnect_versions_and_close_does_not_own_stage_operation(
    config, tmp_path, monkeypatch
):
    from devdash import __version__
    from devdash.gui import Window, gui_instance

    socket = tmp_path / "core.sock"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    with gui_instance(socket) as first:
        assert first
        with gui_instance(socket) as duplicate:
            assert not duplicate
        with gui_instance(socket, new_window=True) as explicit:
            assert explicit
    peer = CorePeer(config, socket)
    window = Window(socket)
    errors = []
    window.report_callback_exception = lambda *values: errors.append(values)

    def wait(predicate):
        deadline = time.monotonic() + 6
        while not predicate():
            assert time.monotonic() < deadline, window.status.cget("text")
            window.update()
            time.sleep(0.01)

    async def block():
        peer.backend.block["camera"] = asyncio.Event()

    def start():
        peer.run(block())
        window.tree.selection_set(json.dumps(["stage", "sensors"]))
        window.tree.event_generate("<<TreeviewSelect>>")
        window.update()
        window.buttons["start"].invoke()
        wait(lambda: window.snapshot["run"]["status"] == "running" and len(peer.backend.calls) >= 2)

    try:
        wait(lambda: window.connected)
        assert not peer.backend.calls
        start()
        instance = window.snapshot["core"]["instance"]
        calls = list(peer.backend.calls)
        peer.run(peer.disconnect())
        wait(lambda: not window.connected)
        assert all(button.instate(["disabled"]) for button in window.buttons.values())
        assert window.next_button.instate(["disabled"])
        assert peer.engine.busy and peer.backend.calls == calls
        peer.run(peer.connect())
        wait(lambda: window.connected)
        assert window.snapshot["core"]["instance"] == instance
        peer.run(peer.restart())
        wait(lambda: window.connected and window.snapshot["core"]["instance"] != instance)
        assert window.snapshot["run"]["status"] == ""
        assert "previous operation outcome unknown" in window.error.cget("text")
        assert peer.backend.calls == calls
        peer.run(peer.metadata(version="0.2.99"))
        wait(lambda: "version mismatch" in window.status.cget("text"))
        peer.run(peer.metadata(protocol=1))
        wait(lambda: not window.connected)
        assert "Incompatible Core IPC" in window.status.cget("text")
        peer.run(peer.metadata(protocol=3, version=__version__))
        wait(lambda: window.connected)
        start()
        window.close()
        assert peer.engine.busy
        assert peer.run(peer.release())["status"] == "completed"
        assert len(peer.backend.calls) == len(calls) + 2
        assert not errors
    finally:
        window.close()
        peer.close()


def test_gui_displays_lifecycle_health_and_observer_loss_separately(config, tmp_path):
    from devdash.gui import Window
    from devdash.model import Config, observation, readiness

    raw = config.json()
    raw["workloads"]["detector"]["observer"] = {"docker": {"container": "detector-container"}}
    raw["workloads"]["detector"]["controller"] = {
        "kind": "supervisord",
        "socket": "/run/supervisor.sock",
    }
    peer = CorePeer(Config(raw), tmp_path / "core.sock")

    async def publish(value):
        peer.backend.observations["detector"] = value
        await peer.engine.poll()

    def fact(available=True):
        return observation(
            state="active",
            health="unhealthy" if available else None,
            readiness=readiness("not_ready", "docker health", "unhealthy")
            if available
            else readiness(),
            internal=observation(state="active" if available else "unknown"),
            observer_error=None if available else "Docker disconnected",
        )

    peer.run(publish(fact()))
    window = Window(peer.socket)

    def wait(predicate):
        deadline = time.monotonic() + 6
        while not predicate():
            assert time.monotonic() < deadline, window.status.cget("text")
            window.update()
            time.sleep(0.01)

    try:
        wait(lambda: window.connected)
        iid = json.dumps(["workload", "detector"])
        window.tree.selection_set(iid)
        window.tree.event_generate("<<TreeviewSelect>>")
        window.update()
        summary = window.summary.cget("text")
        assert "Execution: active" in summary and "Health: unhealthy" in summary
        assert "Lifecycle Controller: systemd" in summary
        assert "Internal Observer: Docker API" in summary and "supervisord (container)" in summary
        assert window.tree.item(iid, "values") == ("active", "not_ready")
        assert window.buttons["start"].cget("text") == "Start (systemd)"
        assert window.next_button.instate(["disabled"])
        peer.run(publish(fact(False)))
        wait(lambda: "Docker disconnected" in window.summary.cget("text"))
        assert window.tree.item(iid, "values") == ("active", "unknown")
        assert "Health: unknown" in window.summary.cget("text")
        peer.run(publish(fact()))
        wait(lambda: "Health: unhealthy" in window.summary.cget("text"))
        window.buttons["start"].invoke()
        wait(lambda: window.snapshot["run"]["status"] == "completed")
        assert peer.backend.calls == [("detector", "start")]
    finally:
        window.close()
        peer.close()
