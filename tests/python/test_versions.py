"""Compatibility checks are independent of release numbers and never replay mutations."""

import asyncio
import json
from importlib.metadata import version

import pytest
from conftest import Fake

from devdash import __version__
from devdash.engine import Engine
from devdash.ipc import OutcomeUnknown, Server, request
from devdash.protocol import CORE_PROTOCOL, CompatibilityError, check_core


@pytest.fixture
async def core_endpoint(config, backend, tmp_path):
    engine = Engine(config, backend)
    server = Server(engine, tmp_path / "config.yaml")
    socket = tmp_path / "core.sock"
    listener = await asyncio.start_unix_server(server.handle, path=str(socket))
    try:
        yield engine, server, socket
    finally:
        listener.close()
        await listener.wait_closed()
        tasks = [t for t in [engine.task, *server.clients] if t and not t.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_core_and_gui_version_comes_from_package_metadata():
    assert __version__ == version("devdash-python")


@pytest.mark.parametrize("protocol", [None, 0, 1, 2, True, "3"])
async def test_incompatible_raw_client_never_dispatches_operation(core_endpoint, backend, protocol):
    engine, server, socket = core_endpoint
    reader, writer = await asyncio.open_unix_connection(str(socket))
    try:
        writer.write(
            json.dumps(
                dict(
                    command="operate",
                    target="camera",
                    kind="workload",
                    action="start",
                    protocol=protocol,
                    core_instance=engine.core["instance"],
                )
            ).encode()
            + b"\n"
        )
        await writer.drain()
        reply = json.loads(await reader.readline())
        assert not reply["ok"] and "requires IPC" in reply["error"]
        assert reply["core"]["version"] == __version__
        assert not engine.busy and not backend.calls
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.parametrize("command", ["operate", "next", "cancel", "child", "reload"])
async def test_stale_gui_core_instance_is_rejected_before_any_mutation(
    core_endpoint, backend, command
):
    engine, server, socket = core_endpoint
    with pytest.raises(CompatibilityError, match="Core instance changed"):
        await request(socket, {"command": command, "core_instance": "old-core"})
    assert not engine.busy and not backend.calls


@pytest.mark.parametrize("protocol", [None, 1, 2])
async def test_legacy_or_incompatible_core_only_receives_readonly_handshake(tmp_path, protocol):
    socket = tmp_path / "legacy.sock"
    received = []
    tasks = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            value = json.loads(await reader.readline())
            received.append(value["command"])
            reply = {"ok": True, "result": {}}
            if protocol is not None:
                reply["core"] = dict(protocol=protocol, version="99.0", instance="foreign")
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    listener = await asyncio.start_unix_server(handle, path=str(socket))
    try:
        with pytest.raises(CompatibilityError, match="Incompatible Core IPC"):
            await request(
                socket,
                dict(
                    command="operate",
                    target="camera",
                    kind="workload",
                    action="start",
                    core_instance="cached-id",
                ),
            )
        assert received == ["snapshot"]
    finally:
        listener.close()
        await listener.wait_closed()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_handshake_and_operation_are_bound_to_same_connection(core_endpoint, config):
    engine, server, socket = core_endpoint
    replacement_backend = Fake()
    replacement = Engine(config, replacement_backend)
    replacement_server = Server(replacement, socket.with_suffix(".yaml"))
    new_listener = None
    original = server.dispatch

    async def dispatch(value):
        nonlocal new_listener
        result = await original(value)
        if value["command"] == "snapshot" and new_listener is None:
            socket.unlink()
            new_listener = await asyncio.start_unix_server(
                replacement_server.handle, path=str(socket)
            )
        return result

    server.dispatch = dispatch
    try:
        await request(
            socket, dict(command="operate", target="camera", kind="workload", action="start")
        )
        assert (await engine.wait())["status"] == "completed"
        assert engine.backend.calls == [("camera", "start")]
        assert not replacement_backend.calls
    finally:
        if new_listener:
            new_listener.close()
            await new_listener.wait_closed()
        await asyncio.gather(*replacement_server.clients, return_exceptions=True)


async def test_connection_lost_after_dispatch_is_unknown_and_not_retried(core_endpoint, backend):
    engine, server, socket = core_endpoint
    dispatched, release = asyncio.Event(), asyncio.Event()
    original = server.dispatch
    mutations = []

    async def dispatch(value):
        result = await original(value)
        if value["command"] == "operate":
            mutations.append(value)
            dispatched.set()
            await release.wait()
        return result

    server.dispatch = dispatch
    client = asyncio.create_task(
        request(socket, dict(command="operate", target="camera", kind="workload", action="start"))
    )
    try:
        await asyncio.wait_for(dispatched.wait(), 2)
        # Abrupt Core-side connection loss before acknowledgement.
        for task in list(server.clients):
            task.cancel()
        with pytest.raises(OutcomeUnknown, match="outcome unknown; not retried"):
            await asyncio.wait_for(client, 2)
        assert len(mutations) == 1
        await engine.wait()
        assert backend.calls == [("camera", "start")]
    finally:
        release.set()
        await asyncio.gather(client, return_exceptions=True)


def test_package_version_difference_is_not_protocol_incompatibility():
    check_core(dict(version="100.5.2", protocol=CORE_PROTOCOL, instance="current"))
