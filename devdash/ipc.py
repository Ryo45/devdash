"""Private local JSON IPC. GUI disconnection never stops an operation."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import signal
from pathlib import Path

import yaml

from .backend import Backends
from .engine import Engine
from .model import Config

LIMIT = 4 * 1024 * 1024


def default_socket():
    return (
        Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/devdash-{os.getuid()}"))
        / "devdash-python/core.sock"
    )


def default_state():
    return (
        Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        / "devdash-python/approvals.json"
    )


async def request(path, command):
    reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(path), limit=LIMIT), 5)
    try:
        writer.write(json.dumps(command).encode() + b"\n")
        await writer.drain()
        reply = json.loads(await asyncio.wait_for(reader.readline(), 30))
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error", "Core request failed"))
        return reply["result"]
    finally:
        writer.close()
        await writer.wait_closed()


class Server:
    def __init__(self, engine, config_path):
        self.engine, self.config_path = engine, Path(config_path)
        self.clients = set()

    async def dispatch(self, value):
        engine = self.engine
        command = value.get("command")
        if command == "snapshot":
            return engine.snapshot()
        if command == "operate":
            engine.launch(value["target"], value.get("group", True), value["action"])
        elif command == "child":
            engine.launch_child(value["parent"], value["child"], value["epoch"], value["action"])
        elif command == "approve":
            engine.approve(value["gate"])
        elif command == "cancel":
            engine.request_cancel()
        elif command == "inspect":
            return await engine.inspect(value["target"], value.get("action", "restart"))
        elif command == "scan":
            return await engine.backend.scan(value.get("scope", "user"))
        elif command == "logs":
            return await engine.backend.logs(engine.config.workloads[value["target"]])
        elif command == "import":
            if engine.busy:
                raise ValueError("Import is unavailable during an operation")
            available = set(await engine.backend.scan(value["scope"]))
            # Scan yields to other clients: recheck before mutating configuration.
            if engine.busy:
                raise ValueError("Import is unavailable during an operation")
            unknown = set(value["units"]) - available
            if unknown:
                raise ValueError("Scan: units not available: " + ", ".join(sorted(unknown)))
            if Config.load(self.config_path).json() != engine.config.json():
                raise ValueError("Configuration changed on disk; restart Core before importing")
            config = Config(engine.config.json())
            ids = [config.import_unit(unit, value["scope"]) for unit in value["units"]]
            backup = self.config_path.with_suffix(self.config_path.suffix + ".bak")
            backup.write_text(self.config_path.read_text())
            temporary = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
            temporary.write_text(yaml.safe_dump(config.json(), sort_keys=False, allow_unicode=True))
            temporary.replace(self.config_path)
            engine.config = config
            engine.backend.systemd.config = config
            engine.invalidate()
            return ids
        else:
            raise ValueError(f"Unknown command: {command}")
        return engine.snapshot()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.clients.add(task)
        try:
            try:
                value = json.loads(await asyncio.wait_for(reader.readline(), 10))
                if not isinstance(value, dict):
                    raise ValueError("Request must be an object")
                result = dict(ok=True, result=await self.dispatch(value))
            except Exception as error:
                result = dict(ok=False, error=str(error))
            encoded = json.dumps(result).encode() + b"\n"
            if len(encoded) > LIMIT:
                encoded = b'{"ok":false,"error":"Response exceeds IPC limit"}\n'
            writer.write(encoded)
            await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            self.clients.discard(task)


async def serve(config_path, socket_path, state_path, backend=None):
    config = Config.load(Path(config_path))
    socket_path, state_path = Path(socket_path), Path(state_path)
    socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if socket_path.parent.stat().st_uid != os.getuid() or socket_path.parent.stat().st_mode & 0o077:
        raise ValueError("Socket parent must belong to current user and have mode 0700")
    with socket_path.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another Core owns this socket") from None
        backend = backend or Backends(config)
        engine = Engine(
            config, backend, state_path, Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        )
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        server = Server(engine, config_path)
        monitor = None
        listener = None
        try:
            await backend.start()
            socket_path.unlink(missing_ok=True)
            listener = await asyncio.start_unix_server(
                server.handle, path=str(socket_path), limit=LIMIT
            )
            socket_path.chmod(0o600)
            monitor = asyncio.create_task(engine.monitor())
            print(f"Core listening: {socket_path}", flush=True)
            await stop.wait()
        finally:
            if listener:
                listener.close()
                await listener.wait_closed()
            tasks = [t for t in [monitor, engine.task, *server.clients] if t and not t.done()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await backend.close()
            socket_path.unlink(missing_ok=True)
