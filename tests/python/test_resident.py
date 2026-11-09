"""Real Core processes on isolated buses; manager state survives Core death."""

import asyncio
import os
import signal
import sys

import pytest
import yaml
from private_bus import Harness

from devdash.ipc import request


async def snapshot_when(socket, predicate):
    async def poll():
        while True:
            value = await request(socket, {"command": "snapshot"})
            if predicate(value):
                return value
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(poll(), 5)


@pytest.mark.parametrize("core_signal", [signal.SIGTERM, signal.SIGKILL])
@pytest.mark.parametrize("stage", ["idle", "job", "checkpoint", "plan_job"])
async def test_core_restart_observes_without_start_replay_or_stop(
    tmp_path, monkeypatch, core_signal, stage
):
    harness = Harness(tmp_path, monkeypatch)
    process = None
    try:
        await harness.start()
        manager = harness.managers["user"]
        manager.active["same.service"] = True  # Already running before devdash.
        config = tmp_path / "config.yaml"
        source = harness.engine.config.json()
        source["stages"][0]["checks"] = ["Check the actual device"]
        config.write_text(yaml.safe_dump(source))
        socket = tmp_path / "private/core.sock"
        environment = dict(
            os.environ,
            DBUS_SESSION_BUS_ADDRESS=harness.buses["user"].address,
            DBUS_SYSTEM_BUS_ADDRESS=harness.buses["system"].address,
        )
        environment.pop("DISPLAY", None)
        environment.pop("WAYLAND_DISPLAY", None)

        async def start():
            core = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-m",
                "devdash",
                "--socket",
                str(socket),
                "daemon",
                "--config",
                str(config),
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            line = await asyncio.wait_for(core.stdout.readline(), 8)
            assert b"Core listening" in line, line
            return core

        process = await start()
        initial = await snapshot_when(
            socket, lambda value: value["observations"].get("probe", {}).get("state") == "active"
        )
        assert initial["run"]["status"] == "" and manager.calls == []
        if stage in ("job", "plan_job"):
            manager.hold_job = True
        if stage in ("checkpoint", "plan_job"):
            preview = await request(socket, dict(command="plan_preview", kind="startup"))
            accepted = await request(socket, dict(command="plan_execute", preview_id=preview["id"]))
        else:
            accepted = await request(
                socket, dict(command="operate", target="robot", kind="stage", action="start")
            )
        assert accepted["run"]["status"] in ("running", "cancelling")
        before = await snapshot_when(
            socket,
            lambda value: (
                value["run"]["status"] == "completed"
                if stage == "idle"
                else bool((value.get("plan") or {}).get("checkpoint"))
                if stage == "checkpoint"
                else bool(manager.calls)
            ),
        )
        assert before["run"]["status"] == (
            "completed" if stage == "idle" else "running"
        )  # Accepted never means succeeded.
        assert manager.calls == [("StartUnit", "same.service")]
        process.send_signal(core_signal)
        await asyncio.wait_for(process.wait(), 5)
        assert process.returncode == (0 if core_signal == signal.SIGTERM else -signal.SIGKILL)
        calls = list(manager.calls)
        process = await start()
        restored = await snapshot_when(
            socket, lambda value: value["observations"].get("probe", {}).get("state") == "active"
        )
        assert restored["core"]["instance"] != initial["core"]["instance"]
        assert restored["run"]["status"] == "" and restored["run"]["progress"] == []
        assert restored["plan"] is None
        assert "approved" not in restored and "waiting_gate" not in restored["run"]
        assert manager.calls == calls  # No resume, stop, restart, or repair.
        assert harness.managers["system"].calls == []
        assert restored["observations"]["after"]["state"] == "inactive"
        assert manager.active["same.service"]
    finally:
        if process and process.returncode is None:
            process.terminate()
            await asyncio.wait_for(process.wait(), 5)
        await harness.close()


async def test_headless_user_bus_uses_runtime_socket_without_desktop(tmp_path, monkeypatch):
    from dbus_fast import BusType
    from dbus_fast.aio import MessageBus

    from devdash.adapters import systemd

    harness = Harness(tmp_path, monkeypatch)
    user_bus = harness.buses["user"]
    user_bus.path = tmp_path / "bus"
    user_bus.address = f"unix:path={user_bus.path}"
    for key in ("DBUS_SESSION_BUS_ADDRESS", "DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    def bus_factory(*, bus_type, **options):
        if bus_type == BusType.SESSION:
            assert options["bus_address"] == user_bus.address
            return MessageBus(bus_address=options["bus_address"])
        return MessageBus(bus_address=harness.buses["system"].address)

    monkeypatch.setattr(systemd, "MessageBus", bus_factory)
    try:
        await harness.start()
        assert harness.backend.health()["user"]["available"]
        assert harness.managers["user"].subscriptions == 1
        assert not harness.managers["user"].calls
    finally:
        await harness.close()
