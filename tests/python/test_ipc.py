import asyncio
import socket as sockets
import sys

import pytest

from devdash.ipc import request


async def test_real_daemon_disconnect_restart_persistence_and_private_socket(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("stages: [{id: robot, workloads: []}]\n")
    socket = tmp_path / "private" / "core.sock"

    async def start():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "devdash",
            "--socket",
            str(socket),
            "daemon",
            "--config",
            str(config),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        line = await asyncio.wait_for(process.stdout.readline(), 15)
        assert b"Core listening" in line, (
            await process.stderr.read() if process.returncode is not None else line
        )
        return process

    process = await start()
    try:
        assert socket.stat().st_mode & 0o777 == 0o600
        assert socket.parent.stat().st_mode & 0o777 == 0o700
        second = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "devdash",
            "--socket",
            str(socket),
            "daemon",
            "--config",
            str(config),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, error = await asyncio.wait_for(second.communicate(), 5)
        assert second.returncode != 0 and b"Another Core owns this socket" in error
        assert (await request(socket, {"command": "snapshot"}))["run"]["status"] == ""
        await request(socket, dict(command="operate", target="robot", kind="stage", action="start"))
        # Requests disconnect; dispatched operations finish without client ownership.
        for _ in range(50):
            if (await request(socket, {"command": "snapshot"}))["run"]["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        assert (await request(socket, {"command": "snapshot"}))["run"]["status"] == "completed"
        process.terminate()
        await asyncio.wait_for(process.wait(), 5)
        assert not socket.exists()
        abandoned = sockets.socket(sockets.AF_UNIX, sockets.SOCK_STREAM)
        abandoned.bind(str(socket))
        abandoned.close()
        process = await start()
        snapshot = await request(socket, {"command": "snapshot"})
        assert "approved" not in snapshot
        assert snapshot["run"]["status"] == ""  # Never auto-resume an operation.
        # Production CLI can communicate with this daemon too.
        cli = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "devdash",
            "--socket",
            str(socket),
            "status",
            stdout=asyncio.subprocess.PIPE,
        )
        out, _ = await cli.communicate()
        assert cli.returncode == 0 and b"stages" in out
        with pytest.raises(RuntimeError, match="Unknown command"):
            await request(socket, {"command": "invalid"})
    finally:
        if process.returncode is None:
            process.terminate()
            await asyncio.wait_for(process.wait(), 5)
