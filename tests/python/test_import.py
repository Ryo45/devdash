"""Scan is readonly; imports edit source, never a generated runtime file."""

import asyncio
import sys

import pytest
import yaml
from conftest import Fake

from devdash.engine import Engine
from devdash.generator import generate
from devdash.ipc import Server, request
from devdash.model import Config


@pytest.fixture
async def editor_core(config, tmp_path):
    source = tmp_path / "source.yaml"
    source.write_text(yaml.safe_dump(config.json()))
    generate(config, tmp_path / "output")
    backend = Fake()
    backend.systemd = backend
    backend.config = config
    engine = Engine(config, backend)
    server = Server(engine, tmp_path / "output/current/devdash.yaml")
    socket = tmp_path / "core.sock"
    listener = await asyncio.start_unix_server(server.handle, path=str(socket))
    try:
        yield engine, backend, server, socket, source
    finally:
        listener.close()
        await listener.wait_closed()
        for task in [engine.task, *server.clients]:
            if task and not task.done():
                task.cancel()
        await asyncio.gather(
            *(t for t in [engine.task, *server.clients] if t), return_exceptions=True
        )


async def test_scan_and_source_import_preserve_runtime_until_explicit_generation_reload(
    editor_core, tmp_path
):
    engine, backend, server, socket, source = editor_core
    original = engine.config.json()
    source_before = source.read_text()
    failed = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "devdash",
        "--socket",
        str(socket),
        "source-import",
        str(source),
        "system",
        "importable.service",
        "missing.service",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, error = await failed.communicate()
    assert failed.returncode != 0
    assert b"Unit unavailable" in error
    assert source.read_text() == source_before and engine.config.json() == original
    assert await request(socket, dict(command="scan", scope="system")) == ["importable.service"]
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "devdash",
        "--socket",
        str(socket),
        "source-import",
        str(source),
        "system",
        "importable.service",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, error = await process.communicate()
    assert process.returncode == 0, error.decode()
    assert "importable" in Config.load(source).workloads
    assert engine.config.json() == original and not backend.calls
    generate(Config.load(source), tmp_path / "output")
    assert engine.config.json() == original
    await request(socket, dict(command="reload"))
    assert "importable" in engine.config.workloads and not backend.calls and not engine.observations
    with pytest.raises(RuntimeError, match="Unknown command"):
        await request(socket, dict(command="import", scope="system", units=["other.service"]))


async def test_reload_rechecks_busy_after_resolution_and_preserves_config(
    editor_core, config, tmp_path
):
    engine, backend, server, socket, source = editor_core
    raw = config.json()
    raw["workloads"]["new"] = dict(backend="systemd", unit="new.service")
    generate(Config(raw), tmp_path / "output")
    entered, release = asyncio.Event(), asyncio.Event()

    async def resolving(work):
        entered.set()
        await release.wait()

    backend.validate_registration = resolving
    reload_task = asyncio.create_task(request(socket, dict(command="reload")))
    await entered.wait()
    backend.block["camera"] = asyncio.Event()
    engine.launch("camera", False, "start")
    release.set()
    with pytest.raises(RuntimeError, match="changed"):
        await reload_task
    assert "new" not in engine.config.workloads
    backend.block["camera"].set()
    await engine.wait()


async def test_client_disconnect_does_not_cancel_dispatched_stage(editor_core):
    from conftest import until

    engine, backend, _, socket, _ = editor_core
    backend.block["camera"] = asyncio.Event()
    await request(socket, dict(command="operate", kind="stage", target="sensors", action="start"))
    await until(lambda: len(backend.calls) == 2)
    assert (await request(socket, dict(command="snapshot")))["run"]["status"] == "running"
    backend.block["camera"].set()
    assert (await engine.wait())["status"] == "completed"


async def test_reload_discards_inflight_observations_from_previous_configuration(
    editor_core, config, tmp_path
):
    engine, backend, _, socket, _ = editor_core
    entered, release = asyncio.Event(), asyncio.Event()
    original = backend.observe

    async def blocked(work):
        entered.set()
        await release.wait()
        return await original(work)

    backend.observe = blocked
    pending = asyncio.create_task(engine.poll())
    await entered.wait()
    raw = config.json()
    raw["stages"] = []
    raw["workloads"] = {}
    generate(Config(raw), tmp_path / "output")
    await request(socket, dict(command="reload"))
    release.set()
    await pending
    assert not engine.observations and not engine.known
    assert engine.config.workloads == {} and not backend.calls


async def test_reload_clears_results_for_redefined_stage_without_lifecycle(editor_core, tmp_path):
    engine, backend, _, socket, _ = editor_core
    engine.launch("sensors", True, "start")
    await engine.wait()
    calls = list(backend.calls)
    await request(socket, dict(command="reload"))
    assert engine.run["status"] == "" and not engine.run["progress"]
    assert backend.calls == calls


async def test_old_gui_configuration_revision_cannot_start_new_next_target(editor_core):
    from devdash.protocol import CompatibilityError

    engine, backend, _, socket, _ = editor_core
    revision = engine.config_revision
    await request(socket, dict(command="reload"))
    with pytest.raises(CompatibilityError, match="Configuration changed"):
        await request(socket, dict(command="next", after="sensors", config_revision=revision))
    assert not backend.calls
