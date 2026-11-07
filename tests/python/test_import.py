"""Scan/Import restores the real IPC contract, not just Config helpers."""

import asyncio

import pytest
import yaml
from conftest import until

from devdash.engine import Engine
from devdash.ipc import Server, request
from devdash.model import Config


@pytest.fixture
async def ipc_fixture(config, backend, tmp_path):
    path, socket = tmp_path / "config.yaml", tmp_path / "core.sock"
    path.write_text(yaml.safe_dump(config.json(), sort_keys=False))
    backend.systemd = backend
    backend.config = config
    engine = Engine(config, backend)
    service = Server(engine, path)
    listener = await asyncio.start_unix_server(service.handle, path=str(socket))
    try:
        yield engine, backend, socket, path
    finally:
        listener.close()
        await listener.wait_closed()
        tasks = [t for t in [engine.task, *service.clients] if t and not t.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_clients_disconnect_without_losing_gate_and_import_is_explicit(ipc_fixture):
    engine, backend, socket, path = ipc_fixture
    before = path.read_bytes()
    assert await request(socket, {"command": "scan", "scope": "user"}) == ["importable.service"]
    assert path.read_bytes() == before
    assert "importable" not in engine.config.workloads
    await request(socket, dict(command="operate", target="robot", group=True, action="start"))
    await until(lambda: engine.run["waiting_gate"] == "ready")
    assert (await request(socket, {"command": "snapshot"}))["run"]["waiting_gate"] == "ready"
    with pytest.raises(RuntimeError, match="Another"):
        await request(socket, dict(command="operate", target="camera", group=False, action="start"))
    await request(socket, {"command": "approve", "gate": "ready"})
    assert (await asyncio.wait_for(engine.wait(), 2))["status"] == "completed"
    ids = await request(
        socket, {"command": "import", "scope": "system", "units": ["importable.service"]}
    )
    assert ids == ["importable"]
    assert Config.load(path).workloads["importable"]["scope"] == "system"
    assert path.with_suffix(".yaml.bak").read_bytes() == before
    after = path.read_bytes()
    with pytest.raises(RuntimeError, match="not found|not available|Scan"):
        await request(socket, {"command": "import", "scope": "user", "units": ["unknown.service"]})
    assert path.read_bytes() == after
    assert "unknown" not in engine.config.workloads


async def test_import_batch_is_atomic_if_any_selected_unit_is_unknown(ipc_fixture):
    engine, backend, socket, path = ipc_fixture
    before = path.read_bytes()
    with pytest.raises(RuntimeError):
        await request(
            socket,
            {
                "command": "import",
                "scope": "user",
                "units": ["importable.service", "unknown.service"],
            },
        )
    assert path.read_bytes() == before and not path.with_suffix(".yaml.bak").exists()
    assert "importable" not in engine.config.workloads


async def test_import_preserves_manual_edits(ipc_fixture):
    engine, backend, socket, path = ipc_fixture
    edited = "# hand edited\n" + path.read_text().replace("camera.service", "edited.service")
    path.write_text(edited)
    with pytest.raises(RuntimeError, match="changed on disk"):
        await request(
            socket, {"command": "import", "scope": "user", "units": ["importable.service"]}
        )
    assert path.read_text() == edited and not path.with_suffix(".yaml.bak").exists()
    assert engine.config.workloads["camera"]["unit"] == "camera.service"


async def test_import_does_not_mutate_config_when_operation_starts_during_scan(ipc_fixture):
    engine, backend, socket, path = ipc_fixture
    entered, release = asyncio.Event(), asyncio.Event()

    async def scan(scope):
        entered.set()
        await release.wait()
        return ["importable.service"]

    backend.scan = scan
    before = path.read_bytes()
    imported = asyncio.create_task(
        request(socket, {"command": "import", "scope": "user", "units": ["importable.service"]})
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        engine.launch("robot", True, "start")
        await until(lambda: engine.run["waiting_gate"] == "ready")
        release.set()
        with pytest.raises(RuntimeError, match="during an operation"):
            await imported
        assert path.read_bytes() == before
        assert "importable" not in engine.config.workloads
    finally:
        release.set()
        await asyncio.gather(imported, return_exceptions=True)
        if engine.busy:
            engine.request_cancel()
            await engine.wait()


async def test_import_rechecks_busy_after_registration_resolution(ipc_fixture):
    engine, backend, socket, path = ipc_fixture
    entered, release = asyncio.Event(), asyncio.Event()

    async def validate(work):
        entered.set()
        await release.wait()

    backend.validate_registration = validate
    before = path.read_bytes()
    imported = asyncio.create_task(
        request(socket, {"command": "import", "scope": "user", "units": ["importable.service"]})
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        engine.launch("robot", True, "start")
        await until(lambda: engine.run["waiting_gate"] == "ready")
        release.set()
        with pytest.raises(RuntimeError, match="during an operation"):
            await imported
        assert path.read_bytes() == before and "importable" not in engine.config.workloads
    finally:
        release.set()
        await asyncio.gather(imported, return_exceptions=True)
        if engine.busy:
            engine.request_cancel()
            await engine.wait()
