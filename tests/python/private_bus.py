"""A real, activation-free D-Bus plus a small systemd protocol peer.

The two buses are isolated from the host. No environment bus addresses are changed.
Blocking points are Events, so tests choose message ordering without timing sleeps.
"""

import asyncio
from contextlib import suppress
from html import escape

from dbus_fast import BusType, Message, MessageFlag, MessageType, Variant
from dbus_fast.aio import MessageBus

from devdash.adapters import systemd
from devdash.engine import Engine
from devdash.model import Config


class PrivateBus:
    def __init__(self, directory, name):
        self.path = directory / f"{name}.sock"
        self.config = directory / f"{name}.conf"
        self.process = None
        self.address = f"unix:path={self.path}"

    async def start(self):
        self.path.unlink(missing_ok=True)
        self.config.write_text(
            f'<busconfig><type>session</type><listen>{escape(self.address)}</listen><auth>EXTERNAL</auth><policy context="default"><allow own="*"/><allow send_destination="*"/><allow receive_sender="*"/></policy></busconfig>'
        )
        self.process = await asyncio.create_subprocess_exec(
            "dbus-daemon",
            "--nofork",
            "--nopidfile",
            "--print-address=1",
            f"--config-file={self.config}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        address = await asyncio.wait_for(self.process.stdout.readline(), 3)
        if not address:
            raise RuntimeError((await self.process.stderr.read()).decode())
        # Like /run/user/UID/bus: reconnect to the stable path, not an old GUID.
        self.address = address.decode().strip().split(",guid=")[0]

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            await asyncio.wait_for(self.process.wait(), 3)


class Manager:
    def __init__(self, address):
        self.address = address
        self.bus = None
        self.calls = []
        self.tasks = set()
        self.errors = []
        self.subscriptions = 0
        self.active = {}
        self.early = True
        self.result = "done"
        self.deny = False
        self.hold_job = False
        self.burst = 0
        self.paths = {}
        self.block = {}
        self.entered = {key: asyncio.Event() for key in ("subscribe", "load", "read", "reply")}

    def pause(self, key):
        self.entered[key].clear()
        self.block[key] = asyncio.Event()
        return self.block[key]

    async def checkpoint(self, key):
        if event := self.block.pop(key, None):
            self.entered[key].set()
            await event.wait()

    async def start(self, named=True):
        self.bus = await MessageBus(bus_address=self.address).connect()
        self.bus.add_message_handler(self.message)
        if named:
            await self.bus.request_name(systemd.SERVICE)
        return self

    def message(self, message):
        if message.message_type != MessageType.METHOD_CALL or message.path is None:
            return None
        task = asyncio.create_task(self.answer(message))
        self.tasks.add(task)
        task.add_done_callback(self.finished)
        return True

    def finished(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            self.errors.append(error)

    async def answer(self, message):
        try:
            member = message.member
            if member == "Subscribe":
                await self.checkpoint("subscribe")
                self.subscriptions += 1
                reply = Message.new_method_return(message)
            elif member == "LoadUnit":
                await self.checkpoint("load")
                unit = message.body[0]
                path = systemd.PATH + "/unit/" + unit.replace(".", "_2e").replace("-", "_2d")
                self.paths[path] = unit
                reply = Message.new_method_return(message, "o", [path])
            elif member == "GetAll":
                unit = self.paths[message.path]
                if message.body[0] == systemd.SERVICE + ".Unit":
                    await self.checkpoint("read")
                    props = {name: Variant("as", []) for name, *_ in systemd.PROPERTIES}
                    props.update(
                        Id=Variant("s", unit),
                        LoadState=Variant("s", "loaded"),
                        ActiveState=Variant("s", "active" if self.active.get(unit) else "inactive"),
                        SubState=Variant("s", "running" if self.active.get(unit) else "dead"),
                        InvocationID=Variant("ay", b"\0" * 16),
                    )
                else:
                    props = {"MainPID": Variant("u", 0)}
                reply = Message.new_method_return(message, "a{sv}", [props])
            elif member in ("StartUnit", "StopUnit", "RestartUnit"):
                assert message.flags & MessageFlag.ALLOW_INTERACTIVE_AUTHORIZATION
                unit = message.body[0]
                self.calls.append((member, unit))
                if self.deny:
                    await self.bus.send(
                        Message.new_error(
                            message,
                            "org.freedesktop.DBus.Error.AccessDenied",
                            "fixture denied manage-units",
                        )
                    )
                    return
                number = len(self.calls)
                path = f"{systemd.PATH}/job/{number}"
                for index in range(self.burst):
                    await self.signal(
                        "UnitNew", "so", [f"noise-{index}.service", systemd.PATH + "/unit/noise"]
                    )
                    await self.job(1000 + index, unit="unregistered.service")
                if self.early and not self.hold_job:
                    self.active[unit] = member != "StopUnit"
                    await self.job(number, unit=unit, result=self.result)
                await self.checkpoint("reply")
                await self.bus.send(Message.new_method_return(message, "o", [path]))
                if not self.early and not self.hold_job:
                    self.active[unit] = member != "StopUnit"
                    await self.job(number, unit=unit, result=self.result)
                return
            else:
                reply = Message.new_error(
                    message, "org.freedesktop.DBus.Error.UnknownMethod", member
                )
            await self.bus.send(reply)
        except asyncio.CancelledError:
            raise

    async def signal(self, member, signature, body):
        await self.bus.send(
            Message.new_signal(systemd.PATH, systemd.MANAGER, member, signature, body)
        )

    async def job(self, number, unit="same.service", result="done"):
        await self.signal(
            "JobRemoved", "uoss", [number, f"{systemd.PATH}/job/{number}", unit, result]
        )

    async def close(self):
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.bus:
            if self.bus.connected:
                self.bus.disconnect()
            with suppress(Exception):
                await self.bus.wait_for_disconnect()


class Harness:
    def __init__(self, directory, monkeypatch):
        self.buses = {scope: PrivateBus(directory, scope) for scope in ("user", "system")}
        self.managers = {}
        self.peers = []
        self.backend = None
        self.engine = None
        config = Config(
            {
                "workloads": {
                    "probe": {"backend": "systemd", "unit": "same.service"},
                    "system_probe": {
                        "backend": "systemd",
                        "scope": "system",
                        "unit": "same.service",
                    },
                    "after": {"backend": "systemd", "unit": "after.service"},
                },
                "groups": {
                    "robot": {
                        "steps": [{"workload": "probe"}, {"gate": "ready"}, {"workload": "after"}]
                    }
                },
                "gates": {"ready": {"type": "manual"}},
            }
        )

        def bus_factory(*, bus_type):
            scope = "user" if bus_type == BusType.SESSION else "system"
            return MessageBus(bus_address=self.buses[scope].address)

        monkeypatch.setattr(systemd, "MessageBus", bus_factory)
        self.backend = systemd.Systemd(config)
        self.engine = Engine(config, self.backend)

    async def peer(self, scope, named=True):
        peer = await Manager(self.buses[scope].address).start(named)
        self.peers.append(peer)
        self.managers[scope] = peer
        return peer

    async def start(self, named=True):
        for scope, bus in self.buses.items():
            await bus.start()
            await self.peer(scope, named)
        await self.backend.start()
        return self

    async def close(self):
        if self.engine.task and not self.engine.task.done():
            self.engine.task.cancel()
            await asyncio.gather(self.engine.task, return_exceptions=True)
        await self.backend.close()
        for peer in self.peers:
            await peer.close()
        for bus in self.buses.values():
            await bus.close()
        assert not [error for peer in self.peers for error in peer.errors], (
            "Private D-Bus peer failed"
        )
