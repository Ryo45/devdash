"""Use the real Bridge socket handler without starting ROS or any nodes."""

import asyncio
import importlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from devdash.adapters.docker import host_rpc
from devdash.backend import Backends
from devdash.model import Config, observation


@pytest.fixture
async def bridge_peer(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "bridges/roslaunch"))
    bridge = importlib.import_module("devdash_roslaunch.bridge")
    registry = bridge.Registry()
    child = dict(
        id="launch:1",
        name="fixture",
        epoch=registry.epoch,
        state="running",
        kind="service",
        pid=None,
        pid_namespace="bridge",
        cpu_percent=None,
        memory_bytes=None,
        exit_code=None,
        actions=["stop"],
        reason="fixture",
    )
    registry.processes["launch:1"] = bridge.Process(None, child, asyncio.Event())
    original = registry.snapshot
    info = {"version": "7.4.2", "protocol": 1}  # Does not need to match Core release.

    def snapshot():
        value = original()
        value["bridge"] = dict(info)
        return value

    registry.snapshot = snapshot
    registry.operate = AsyncMock(side_effect=lambda request: snapshot())
    socket = tmp_path / "bridge.sock"
    tasks = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            await bridge.handle(registry, reader, writer)
        finally:
            tasks.discard(task)

    listener = await asyncio.start_unix_server(handle, path=str(socket))
    controller = dict(kind="roslaunch", socket=str(socket), python=sys.executable)
    try:
        yield registry, info, controller, bridge
    finally:
        listener.close()
        await listener.wait_closed()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("protocol", [None, 2, True])
async def test_rpc_client_never_sends_mutation_to_legacy_or_incompatible_bridge(
    bridge_peer, protocol
):
    registry, info, controller, bridge = bridge_peer
    if protocol is None:
        info.pop("protocol")
    else:
        info["protocol"] = protocol
    # Metadata remains inspectable even if incompatible.
    snapshot = await host_rpc(controller, {"command": "snapshot"})
    assert snapshot["bridge"].get("protocol") == protocol
    with pytest.raises(RuntimeError, match="Incompatible ROS Bridge IPC"):
        await host_rpc(
            controller, dict(command="operate", id="launch:1", epoch=registry.epoch, action="stop")
        )
    registry.operate.assert_not_awaited()


async def test_compatible_bridge_can_have_an_independent_package_version(bridge_peer):
    registry, info, controller, bridge = bridge_peer
    reply = await host_rpc(
        controller, dict(command="operate", id="launch:1", epoch=registry.epoch, action="stop")
    )
    assert reply["bridge"]["version"] == "7.4.2"
    registry.operate.assert_awaited_once()


async def test_oneshot_client_refuses_stale_bridge_epoch(bridge_peer):
    registry, info, controller, bridge = bridge_peer
    with pytest.raises(RuntimeError, match="Bridge changed"):
        await host_rpc(
            controller, dict(command="operate", id="launch:1", epoch="old-bridge", action="stop")
        )
    registry.operate.assert_not_awaited()


@pytest.mark.parametrize("protocol", [None, 0, 2, True])
async def test_bridge_rejects_incompatible_raw_mutations_before_registry(bridge_peer, protocol):
    import json

    registry, info, controller, bridge = bridge_peer
    reader, writer = await asyncio.open_unix_connection(controller["socket"])
    try:
        writer.write(
            json.dumps(
                dict(
                    command="operate",
                    id="launch:1",
                    epoch=registry.epoch,
                    action="stop",
                    protocol=protocol,
                )
            ).encode()
            + b"\n"
        )
        await writer.drain()
        reply = json.loads(await reader.readline())
        assert not reply["ok"] and "requires IPC" in reply["error"]
        registry.operate.assert_not_awaited()
    finally:
        writer.close()
        await writer.wait_closed()


async def test_core_observation_exposes_bridge_version_and_disables_incompatible_children(
    bridge_peer,
):
    registry, info, controller, bridge = bridge_peer
    config = Config(
        {
            "workloads": {
                "launch": {
                    "backend": "systemd",
                    "unit": "fixture.service",
                    "controller": controller,
                }
            }
        }
    )
    backend = Backends(config)

    async def observe(work):
        return observation(state="active")

    backend.systemd.observe = observe
    try:
        value = await backend.observe(config.workloads["launch"])
        assert value["controller"]["compatible"]
        assert value["controller"]["version"] == "7.4.2"
        old_child = value["children"][0]
        registry.epoch = "replacement"
        registry.processes["launch:1"].child["epoch"] = registry.epoch
        with pytest.raises(ValueError, match="changed"):
            await backend.operate_child(
                config.workloads["launch"], old_child["id"], old_child["epoch"], "stop"
            )
        registry.operate.assert_not_awaited()
        info["protocol"] = 2
        value = await backend.observe(config.workloads["launch"])
        assert value["controller"]["version"] == "7.4.2" and not value["controller"]["compatible"]
        assert value["state"] == "active" and value["children"] == []
        assert "Incompatible ROS Bridge IPC" in value["children_error"]
    finally:
        await backend.close()
