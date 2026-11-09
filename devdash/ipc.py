"""Private local JSON IPC. GUI disconnection never stops an operation."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import signal
from contextlib import suppress
from pathlib import Path

from .backend import Backends
from .engine import Engine
from .inspection import inspect
from .model import Config, systemd_key
from .protocol import CORE_PROTOCOL, MUTATIONS, CompatibilityError, check_core, check_request

LIMIT = 4 * 1024 * 1024


def default_socket():
    return (
        Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/devdash-{os.getuid()}"))
        / "devdash-python/core.sock"
    )


class OutcomeUnknown(RuntimeError):
    """A mutation may have reached Core. Never automatically retry it."""


async def request(path, command):
    value = dict(command, protocol=CORE_PROTOCOL)
    writer = None
    mutation_sent = False
    mutating = value.get("command") in MUTATIONS
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(path), limit=LIMIT), 5
        )

        async def exchange(payload):
            nonlocal mutation_sent
            writer.write(json.dumps(payload).encode() + b"\n")
            mutation_sent |= payload.get("command") in MUTATIONS
            await writer.drain()
            raw = await asyncio.wait_for(reader.readline(), 30)
            if not raw:
                raise ConnectionError("Core closed the connection without a result")
            reply = json.loads(raw)
            if not isinstance(reply, dict):
                raise ValueError("Core reply must be an object")
            check_core(reply.get("core"))
            if payload.get("command") in MUTATIONS and reply["core"]["instance"] != payload.get(
                "core_instance"
            ):
                raise CompatibilityError(
                    "Core instance changed; request rejected; refresh before operating"
                )
            if not reply.get("ok"):
                raise RuntimeError(reply.get("error", "Core request failed"))
            if "result" not in reply:
                raise ValueError("Core reply result missing")
            return reply["result"]

        if mutating:
            # Handshake and mutation use one connection: a replacement/legacy
            # Core cannot receive the second frame after a successful check.
            snapshot = await exchange({"command": "snapshot", "protocol": CORE_PROTOCOL})
            if not isinstance(snapshot, dict):
                raise CompatibilityError("Core handshake is not a snapshot")
            check_core(snapshot.get("core"))
            instance = snapshot["core"]["instance"]
            if value.get("core_instance", instance) != instance:
                raise CompatibilityError(
                    "Core instance changed; refresh observations before operating. Request rejected."
                )
            value["core_instance"] = instance
            revision = snapshot["config_revision"]
            if value.get("config_revision", revision) != revision:
                raise CompatibilityError("Configuration changed; refresh before operating")
            value["config_revision"] = revision
        return await exchange(value)
    except (OSError, asyncio.TimeoutError, ValueError) as error:
        if mutation_sent:
            raise OutcomeUnknown(
                f"Core communication lost; operation outcome unknown; not retried: {error}"
            ) from error
        raise
    finally:
        if writer:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()


class Server:
    def __init__(self, engine, config_path):
        self.engine, self.config_path = engine, Path(config_path)
        self.clients = set()

    async def dispatch(self, value):
        engine = self.engine
        check_request(value, engine.core)
        command = value.get("command")
        if command in MUTATIONS and value.get("config_revision") != engine.config_revision:
            raise CompatibilityError("Configuration changed; refresh before operating")
        if command == "snapshot":
            return engine.snapshot()
        if command == "operate":
            if value.get("kind") not in ("stage", "workload"):
                raise ValueError("Operation needs kind=stage or workload")
            engine.launch(value["target"], value["kind"] == "stage", value["action"])
        elif command == "plan_preview":
            return engine.plans.preview(
                value.get("kind"), value.get("from_stage"), value.get("template")
            )
        elif command == "plan_templates":
            return engine.config.plan_templates
        elif command == "plan_execute":
            engine.plans.start(value["preview_id"])
        elif command == "plan_continue":
            await engine.plans.continue_run(value["run_id"], value["token"])
        elif command == "plan_cancel":
            engine.plans.cancel(value["run_id"])
        elif command == "next":
            engine.launch_next(value["after"])
        elif command == "child":
            engine.launch_child(value["parent"], value["child"], value["epoch"], value["action"])
        elif command == "cancel":
            engine.request_cancel()
        elif command == "inspect":
            return await engine.inspect(value["target"], value.get("action", "restart"))
        elif command == "scan":
            return await engine.backend.scan(value.get("scope", "user"))
        elif command == "logs":
            return await engine.backend.logs(engine.config.workloads[value["target"]])
        elif command == "inspect_config":
            candidate = Config(value["config"])
            seeds = {
                key
                for work in candidate.workloads.values()
                if (key := systemd_key(work)) is not None
            }
            graph = await engine.backend.dependencies(seeds)
            return [
                inspect(candidate, stage["id"], "restart", graph, check_declared=False)
                for stage in candidate.stages
            ]
        elif command == "resolve":
            await engine.backend.validate_registration(
                dict(backend="systemd", scope=value["scope"], unit=value["unit"])
            )
            return {"valid": True}
        elif command == "reload":
            return await self.reload_config()
        else:
            raise ValueError(f"Unknown command: {command}")
        return engine.snapshot()

    async def reload_config(self):
        if self.engine.busy:
            raise ValueError("Reload unavailable during an operation")
        candidate = Config.load(self.config_path)
        for work in candidate.workloads.values():
            try:
                await self.engine.backend.validate_registration(work)
            except ValueError:
                raise
            except Exception:
                pass  # Missing/offline units remain registered, with unknown/not_found observations.
        if self.engine.busy or Config.load(self.config_path).json() != candidate.json():
            raise ValueError("Configuration or operation changed during reload; retry")
        self.engine.config = candidate
        self.engine.config_revision += 1
        self.engine.backend.systemd.config = candidate
        self.engine.observations.clear()
        self.engine.known.clear()
        self.engine.observer.internal_known.clear()
        self.engine.observer.observed_at = 0.0
        self.engine.outages.clear()
        self.engine.reports.clear()
        self.engine.run.clear()
        self.engine.run.update(target="", kind="", action="", status="", progress=[], error=None)
        self.engine.plans.previews.clear()
        self.engine.invalidate()
        self.engine.emit(
            "info",
            "Configuration reloaded; fresh observation baseline; no lifecycle operations issued",
            kind="configuration",
        )
        return self.engine.snapshot()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.clients.add(task)
        try:
            # One read-only request, or snapshot handshake + one mutation.
            for _ in range(2):
                raw = await asyncio.wait_for(reader.readline(), 10)
                if not raw:
                    break
                try:
                    value = json.loads(raw)
                    if not isinstance(value, dict):
                        raise ValueError("Request must be an object")
                    result = dict(ok=True, result=await self.dispatch(value), core=self.engine.core)
                except Exception as error:
                    result = dict(ok=False, error=str(error), core=self.engine.core)
                encoded = json.dumps(result).encode() + b"\n"
                if len(encoded) > LIMIT:
                    encoded = (
                        json.dumps(
                            dict(
                                ok=False, error="Response exceeds IPC limit", core=self.engine.core
                            )
                        ).encode()
                        + b"\n"
                    )
                writer.write(encoded)
                await writer.drain()
                if not result["ok"]:
                    break
        except (ConnectionError, asyncio.CancelledError, asyncio.TimeoutError, ValueError):
            pass
        finally:
            writer.close()
            self.clients.discard(task)
            with suppress(ConnectionError):
                await writer.wait_closed()


async def serve(config_path, socket_path, backend=None):
    config = Config.load(Path(config_path))
    socket_path = Path(socket_path)
    socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if socket_path.parent.stat().st_uid != os.getuid() or socket_path.parent.stat().st_mode & 0o077:
        raise ValueError("Socket parent must belong to current user and have mode 0700")
    with socket_path.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another Core owns this socket") from None
        backend = backend or Backends(config)
        engine = Engine(config, backend)
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
