"""Preview/Execute/Continue are separate requests; IDs and config generations are checked."""

import asyncio
import json
import sys

import pytest
from conftest import until

from devdash.engine import Engine
from devdash.ipc import Server, request
from devdash.model import Config


@pytest.fixture
async def plan_core(config, backend, tmp_path):
    raw = config.json()
    raw["stages"][0]["checks"] = ["Verify sensors"]
    raw["stages"][1]["checks"] = ["Verify perception"]
    raw["plan_templates"] = {"startup": {"kind": "startup"}}
    engine = Engine(Config(raw), backend)
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


async def test_cli_preview_then_ipc_execute_continue_and_discard(plan_core, backend):
    engine, _, socket = plan_core
    cli = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "devdash",
        "--socket",
        str(socket),
        "plan",
        "preview",
        "--template",
        "startup",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, error = await cli.communicate()
    assert cli.returncode == 0, error.decode()
    preview = json.loads(output)
    assert len(preview["steps"]) == 5 and not backend.calls and not engine.busy
    await request(socket, dict(command="plan_execute", preview_id=preview["id"]))
    await until(lambda: engine.snapshot()["plan"] and engine.snapshot()["plan"]["checkpoint"])
    run = engine.snapshot()["plan"]
    with pytest.raises(RuntimeError, match="Run changed"):
        await request(
            socket, dict(command="plan_continue", run_id="old", token=run["checkpoint"]["token"])
        )
    await request(
        socket, dict(command="plan_continue", run_id=run["id"], token=run["checkpoint"]["token"])
    )
    await until(
        lambda: (
            engine.snapshot()["plan"]
            and (engine.snapshot()["plan"]["checkpoint"] or {}).get("stage") == "perception"
        )
    )
    assert ("planner", "start") not in backend.calls
    cli = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "devdash",
        "--socket",
        str(socket),
        "plan",
        "cancel",
        run["id"],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, error = await cli.communicate()
    assert cli.returncode == 0, error.decode()
    assert (await engine.wait())["status"] == "cancelled" and engine.plans.active is None
    with pytest.raises(RuntimeError, match="Unknown/consumed"):
        await request(socket, dict(command="plan_execute", preview_id=preview["id"]))
    await request(
        socket, dict(command="operate", kind="workload", target="planner", action="restart")
    )
    assert (await engine.wait())["status"] == "completed"


async def test_fresh_observation_change_rejects_old_continue_and_freezes_reload(plan_core, backend):
    engine, _, socket = plan_core
    preview = await request(socket, dict(command="plan_preview", kind="startup"))
    await request(socket, dict(command="plan_execute", preview_id=preview["id"]))
    await until(lambda: engine.snapshot()["plan"] and engine.snapshot()["plan"]["checkpoint"])
    run = engine.snapshot()["plan"]
    backend.observations["camera"]["invocation_id"] = "replacement"
    with pytest.raises(RuntimeError, match="changed"):
        await request(
            socket,
            dict(command="plan_continue", run_id=run["id"], token=run["checkpoint"]["token"]),
        )
    assert ("detector", "start") not in backend.calls
    with pytest.raises(RuntimeError, match="operation"):
        await request(socket, dict(command="reload"))
    await request(socket, dict(command="plan_cancel", run_id=run["id"]))
    await engine.wait()
