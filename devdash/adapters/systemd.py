"""Direct asyncio D-Bus adapter. systemd remains the process owner."""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from pathlib import Path
from urllib.parse import quote

from dbus_fast import BusType, Message, MessageFlag, MessageType, Variant
from dbus_fast.aio import MessageBus

from ..inspection import PROPERTIES, Graph
from ..model import UnitKey, observation

SERVICE = "org.freedesktop.systemd1"
PATH = "/org/freedesktop/systemd1"
MANAGER = SERVICE + ".Manager"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"


def unpack(value):
    if isinstance(value, Variant):
        return unpack(value.value)
    if isinstance(value, dict):
        return {key: unpack(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [unpack(item) for item in value]
    return value


class BusError(RuntimeError):
    def __init__(self, name, body):
        self.name = name
        super().__init__(f"{name}: {body}")


class Session:
    def __init__(self, backend, scope, generation):
        self.backend, self.scope, self.generation = backend, scope, generation
        self.bus = None
        self.owner = ""
        self.lost = asyncio.Event()
        self.reason = None
        self.published = False
        self.issuing: dict[str, int] = {}
        self.jobs: dict[str, tuple[str, asyncio.Future]] = {}
        self.early: dict[str, tuple[str, str, float]] = {}
        self.aliases: dict[str, str] = {}
        self.recent: dict[str, float] = {}
        self.metadata: dict[str, float] = {}
        self.reloading = False
        self.disconnect_task = None

    async def connect(self):
        options = {"bus_type": BusType.SESSION if self.scope == "user" else BusType.SYSTEM}
        if self.scope == "user" and not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
            options["bus_address"] = "unix:path=" + quote(str(runtime / "bus"), safe="/")
        self.bus = await MessageBus(**options).connect()
        self.owner = (
            await self.call(
                "GetNameOwner",
                "s",
                [SERVICE],
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
            )
        )[0]
        self.bus.add_message_handler(self.message)
        for rule in (
            f"type='signal',sender='{self.owner}',interface='{MANAGER}',path='{PATH}'",
            f"type='signal',sender='org.freedesktop.DBus',interface='org.freedesktop.DBus',member='NameOwnerChanged',arg0='{SERVICE}'",
        ):
            await self.call(
                "AddMatch",
                "s",
                [rule],
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
            )
        await self.call("Subscribe")
        owner = (
            await self.call(
                "GetNameOwner",
                "s",
                [SERVICE],
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
            )
        )[0]
        if owner != self.owner:
            raise RuntimeError("manager changed during subscription")
        self.check()
        self.disconnect_task = asyncio.create_task(self.disconnected())
        return self

    async def disconnected(self):
        try:
            await self.bus.wait_for_disconnect()
        except Exception as error:
            self.fail(f"D-Bus connection lost: {error}")
        else:
            self.fail("D-Bus connection closed")

    def check(self):
        if self.lost.is_set():
            raise RuntimeError(f"{self.scope} manager lost; outcome unknown: {self.reason}")

    def fail(self, reason):
        if self.lost.is_set():
            return
        self.reason = reason
        self.lost.set()
        if not self.published:
            return
        self.backend.revision += 1
        self.backend.event(
            self.scope,
            "disconnected",
            f"{self.scope} manager disconnected: {reason}; pending outcomes unknown",
        )
        # Wake all waiters using the sticky generation token. Don't send CancelJob.

    async def guarded(self, awaitable, timeout=None):
        operation = asyncio.ensure_future(awaitable)
        lost = asyncio.create_task(self.lost.wait())
        try:
            self.check()
            done, _ = await asyncio.wait(
                [operation, lost], timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if operation in done:
                return await operation
            self.check()
            raise TimeoutError("D-Bus metadata request timed out")
        finally:
            for task in (operation, lost):
                if not task.done():
                    task.cancel()
            await asyncio.gather(operation, lost, return_exceptions=True)

    async def call(
        self,
        member,
        signature="",
        body=None,
        *,
        destination=None,
        path=PATH,
        interface=MANAGER,
        interactive=False,
    ):
        self.check()
        reply = await self.guarded(
            self.bus.call(
                Message(
                    destination=destination or self.owner,
                    path=path,
                    interface=interface,
                    member=member,
                    signature=signature,
                    body=body or [],
                    flags=MessageFlag.ALLOW_INTERACTIVE_AUTHORIZATION
                    if interactive
                    else MessageFlag.NONE,
                )
            )
        )
        if reply is None:
            raise RuntimeError("D-Bus reply missing")
        if reply.message_type == MessageType.ERROR:
            hint = (
                "; systemd/Polkit manage-units authorization and a session agent may be required"
                if any(
                    word in (reply.error_name or "")
                    for word in ("AccessDenied", "Auth", "PermissionDenied")
                )
                else ""
            )
            raise BusError(reply.error_name, str(reply.body) + hint)
        return unpack(reply.body)

    async def properties(self, path, interface):
        return (
            await self.call("GetAll", "s", [interface], path=path, interface=PROPERTIES_INTERFACE)
        )[0]

    async def unit(self, name):
        path = (await self.call("LoadUnit", "s", [name]))[0]
        properties = await self.properties(path, SERVICE + ".Unit")
        self.aliases[name] = properties.get("Id", name)
        return path, properties

    def workload(self, unit):
        return next(
            (
                key
                for key, work in self.backend.config.workloads.items()
                if work["backend"] == "systemd"
                and work["scope"] == self.scope
                and self.aliases.get(work["unit"], work["unit"]) == unit
            ),
            None,
        )

    def job_event(self, path, unit, result, observed, correlation):
        self.backend.event(
            self.scope,
            "job",
            f"systemd job {path}: {result} ({correlation}; origin not otherwise proven)",
            unit=unit,
            observed_at=observed,
            correlation=correlation,
            job_path=path,
            job_result=result,
        )

    def message(self, message):
        # No sleeps, I/O, blocking queues or dependency analysis in the reader.
        if message.message_type != MessageType.SIGNAL:
            return None
        if (
            message.sender == "org.freedesktop.DBus"
            and message.interface == "org.freedesktop.DBus"
            and message.member == "NameOwnerChanged"
        ):
            name, old, new = message.body
            if name == SERVICE and old == self.owner and old != new:
                self.fail("manager name owner changed")
            return None
        if self.lost.is_set() or message.sender != self.owner or message.interface != MANAGER:
            return None
        try:
            if message.member == "JobRemoved":
                _, path, unit, result = message.body
                observed = time.monotonic()
                if path in self.jobs:
                    _, future = self.jobs[path]
                    if not future.done():
                        future.set_result(result)
                    self.recent[unit] = observed
                    self.job_event(path, unit, result, observed, "requested")
                elif self.issuing.get(unit):
                    if len(self.early) >= 256:
                        self.fail("job correlation overflow; event history incomplete")
                    else:
                        self.early[path] = (unit, result, observed)
                else:
                    self.job_event(path, unit, result, observed, "unrelated")
            elif message.member in ("UnitNew", "UnitRemoved"):
                unit = message.body[0]
                now = time.monotonic()
                if self.workload(unit) is not None and now - self.metadata.get(unit, -100) >= 5:
                    self.metadata[unit] = now
                    self.backend.event(
                        self.scope,
                        "unit_new" if message.member == "UnitNew" else "unit_removed",
                        "Unit metadata loaded/unloaded; not proof of service/file transition (5s rate limit)",
                        unit=unit,
                    )
            elif message.member == "Reloading":
                self.reloading = bool(message.body[0])
                self.backend.revision += 1
                self.backend.event(
                    self.scope,
                    "reloading" if self.reloading else "reloaded",
                    f"{self.scope} manager {'reloading' if self.reloading else 'reloaded'}; diagnostics invalidated",
                )
        except Exception as error:
            self.fail(f"Invalid manager event: {error}")
        return None

    async def operate(self, work, action, lease):
        # Pin before checking the captured old lease; a new manager never receives
        # the next operation from an interrupted sequence.
        if lease is not None and lease.is_set():
            raise RuntimeError("Manager generation changed; sequence will not resume")
        _, properties = await self.guarded(self.unit(work["unit"]), timeout=5)
        unit = properties["Id"]
        self.check()
        self.issuing[unit] = self.issuing.get(unit, 0) + 1
        path = None
        reply_known = False
        try:
            path = (
                await self.call(
                    {"start": "StartUnit", "stop": "StopUnit", "restart": "RestartUnit"}[action],
                    "ss",
                    [work["unit"], "replace"],
                    interactive=True,
                )
            )[0]
            reply_known = True
            future = asyncio.get_running_loop().create_future()
            self.jobs[path] = (unit, future)
            self.recent[unit] = time.monotonic()
            if path in self.early:
                event_unit, result, observed = self.early.pop(path)
                future.set_result(result)
                self.job_event(path, event_unit, result, observed, "requested")
            result = await self.guarded(future)
            if result != "done":
                raise RuntimeError(f"{action} {self.scope}:{unit}: systemd job result {result}")
        finally:
            if path:
                self.jobs.pop(path, None)
            self.issuing[unit] -= 1
            if not self.issuing[unit]:
                self.issuing.pop(unit)
                for early_path, (event_unit, result, observed) in list(self.early.items()):
                    if event_unit == unit:
                        self.early.pop(early_path)
                        self.job_event(
                            early_path,
                            event_unit,
                            result,
                            observed,
                            "unrelated" if reply_known else "unknown",
                        )

    async def close(self):
        self.fail("connection generation closed")
        if self.bus and self.bus.connected:
            self.bus.disconnect()
        if self.disconnect_task:
            await asyncio.gather(self.disconnect_task, return_exceptions=True)


class Systemd:
    def __init__(self, config):
        self.config = config
        self.sessions: dict[str, Session | None] = {"user": None, "system": None}
        self.errors = {"user": "connecting", "system": "connecting"}
        self.revision = 0
        self.generation = 0
        self.tasks = []
        self.emit = lambda event: None
        self.closed = False

    def event(self, scope, kind, message, unit=None, **facts):
        workload = None
        if unit:
            session = self.sessions[scope]
            workload = session.workload(unit) if session else None
            if workload is None:
                return
        self.emit(dict(scope=scope, kind=kind, message=message, workload=workload, **facts))

    async def connection(self, scope):
        self.generation += 1
        session = Session(self, scope, self.generation)
        try:
            await asyncio.wait_for(session.connect(), 5)
            return session
        except BaseException:
            await session.close()
            raise

    async def start(self):
        async def initial(scope):
            try:
                self.sessions[scope] = await self.connection(scope)
                self.sessions[scope].published = True
                self.revision += 1
            except Exception as error:
                self.errors[scope] = str(error)
            self.tasks.append(asyncio.create_task(self.reconnect(scope)))

        await asyncio.gather(*(initial(scope) for scope in self.sessions))

    async def reconnect(self, scope):
        delay = 1
        while not self.closed:
            session = self.sessions[scope]
            if session:
                await session.lost.wait()
                self.errors[scope] = session.reason
                self.sessions[scope] = None
                await session.close()
            await asyncio.sleep(delay)
            try:
                candidate = await self.connection(scope)
            except Exception as error:
                self.errors[scope] = str(error)
                delay = min(10, delay * 2)
                continue
            candidate.published = True
            self.sessions[scope] = candidate
            self.revision += 1
            self.errors[scope] = None
            self.event(
                scope,
                "reconnected",
                f"{scope} manager monitoring restored; history gap; interrupted operations are not resumed",
            )
            delay = 1

    def session(self, scope):
        session = self.sessions.get(scope)
        if session is None or session.lost.is_set():
            raise RuntimeError(
                f"{scope} manager unavailable; reconnecting: {self.errors.get(scope)}"
            )
        return session

    def lease(self, scope):
        session = self.sessions.get(scope)
        if session and not session.lost.is_set():
            return session.lost
        lost = asyncio.Event()
        lost.set()
        return lost

    def health(self):
        return {
            scope: dict(
                available=bool(session and not session.lost.is_set()),
                reason=session.reason
                if session and session.lost.is_set()
                else self.errors.get(scope)
                if not session
                else None,
            )
            for scope, session in self.sessions.items()
        }

    def recent(self, work):
        session = self.sessions.get(work["scope"])
        if not session:
            return False
        unit = session.aliases.get(work["unit"], work["unit"])
        return bool(
            session.issuing.get(unit)
            or any(key == unit for key, _ in session.jobs.values())
            or time.monotonic() - session.recent.get(unit, -100) < 5
        )

    async def operate(self, work, action, lease=None):
        session = self.session(work["scope"])
        await session.operate(work, action, lease)

    async def observe(self, work):
        session = self.session(work["scope"])
        try:
            path, props = await session.unit(work["unit"])
        except BusError as error:
            if error.name == SERVICE + ".NoSuchUnit":
                return observation(state="not_found", manager_generation=session.generation)
            raise
        if props.get("LoadState") == "not-found":
            return observation(state="not_found", manager_generation=session.generation)
        try:
            service = await session.properties(path, SERVICE + ".Service")
        except (BusError, TimeoutError):
            service = {}
        session.check()
        invocation = bytes(props.get("InvocationID", [])).hex()

        def valid(value):
            return value if value is not None and value != 2**64 - 1 else None

        return observation(
            state=props.get("ActiveState", "unknown"),
            sub_state=props.get("SubState", ""),
            manager_generation=session.generation,
            canonical_unit=props.get("Id"),
            invocation_id=invocation if any(bytes(props.get("InvocationID", []))) else None,
            pid=service.get("MainPID") or None,
            cpu_nsec=valid(service.get("CPUUsageNSec")),
            memory_bytes=valid(service.get("MemoryCurrent")),
        )

    async def scan(self, scope):
        session = self.session(scope)

        async def scan():
            files = (await session.call("ListUnitFiles"))[0]
            loaded = (await session.call("ListUnits"))[0]
            return sorted(
                {row[0].rsplit("/", 1)[-1] for row in files if row[0].endswith(".service")}
                | {row[0] for row in loaded if row[0].endswith(".service")}
            )

        return await asyncio.wait_for(scan(), 5)

    async def dependencies(self, seeds):
        graph = Graph()
        pending, visited = deque(seeds), set()
        deadline = time.monotonic() + 6
        pinned = {}
        for scope in {key.scope for key in seeds}:
            try:
                pinned[scope] = self.session(scope)
            except RuntimeError as error:
                graph.incomplete.append(str(error))
        while pending:
            key = pending.popleft()
            if key in visited:
                continue
            visited.add(key)
            if len(visited) > 256 or time.monotonic() >= deadline:
                graph.incomplete.append("Inspection bounded at 256 unit lookups / 6 seconds")
                break
            try:
                session = pinned[key.scope]
                _, props = await asyncio.wait_for(
                    session.unit(key.unit), min(2, max(0.01, deadline - time.monotonic()))
                )
                canonical = UnitKey(key.scope, props["Id"])
                graph.aliases[key] = canonical
                if canonical in graph.inspected:
                    continue
                if props.get("LoadState") != "loaded":
                    raise RuntimeError(f"LoadState={props.get('LoadState')}")
                for name, relation, inverse, follow in PROPERTIES:
                    if name not in props:
                        if name in {
                            "Wants",
                            "Requires",
                            "BindsTo",
                            "PartOf",
                            "After",
                            "Before",
                            "Conflicts",
                        }:
                            graph.incomplete.append(f"{key.text()}: {name} unavailable")
                        continue
                    for unit in props[name]:
                        other = UnitKey(key.scope, unit)
                        graph.edges.add(
                            (other, relation, canonical)
                            if inverse
                            else (canonical, relation, other)
                        )
                        if follow:
                            pending.append(other)
                for name in ("OnFailure", "OnSuccess", "Triggers", "TriggeredBy"):
                    if props.get(name):
                        graph.incomplete.append(
                            f"{canonical.text()}: dynamic {name} is not simulated"
                        )
                graph.inspected.add(canonical)
            except Exception as error:
                graph.incomplete.append(f"{key.text()}: {error}")
        graph.stale = any(session.lost.is_set() or session.reloading for session in pinned.values())
        return graph

    async def logs(self, work):
        args = ["journalctl", "--no-pager", "--output=short-iso", "-n", "150"]
        args += ["--user-unit" if work["scope"] == "user" else "--unit", work["unit"]]
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        return (stdout + stderr).decode(errors="replace")

    async def close(self):
        self.closed = True
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.gather(
            *(session.close() for session in self.sessions.values() if session),
            return_exceptions=True,
        )
