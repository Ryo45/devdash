import asyncio
import os
import uuid

import pytest

from devdash.adapters.docker import demultiplex, ready
from devdash.backend import Backends
from devdash.engine import Engine
from devdash.model import Config


def test_health_and_docker_frames():
    assert ready({"State": {"Running": True}})
    assert not ready({"State": {"Running": True, "Health": {"Status": "unhealthy"}}})
    assert not ready({"State": {"Running": True, "Paused": True}})
    assert demultiplex(b"\x01\0\0\0\0\0\0\x02ok") == b"ok"
    with pytest.raises(ValueError):
        demultiplex(b"bad")


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("DEVDASH_DOCKER_TEST") != "1",
    reason="set DEVDASH_DOCKER_TEST=1; requires dedicated fixture image",
)
async def test_supervisor_services_health_epochs_and_container_recreation():
    name = "devdash-py-it-" + uuid.uuid4().hex[:10]
    image = os.environ.get("DEVDASH_TEST_IMAGE", "devdash-supervisor-test:4.3.0")

    async def docker(*args):
        proc = await asyncio.create_subprocess_exec(
            "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await asyncio.wait_for(proc.communicate(), 30)
        assert proc.returncode == 0, err.decode()
        return out.decode().strip()

    async def create():
        return await docker(
            "create",
            "--name",
            name,
            "--network",
            "none",
            "--cpus",
            "1",
            "--memory",
            "512m",
            "--label",
            f"com.docker.compose.project={name}",
            "--label",
            "com.docker.compose.service=robot",
            image,
        )

    initial_id = await create()
    config = Config(
        {
            "workloads": {
                "container": {
                    "backend": "docker",
                    "docker": {"compose": {"project": name, "service": "robot"}},
                    "controller": {"kind": "supervisord", "socket": "/tmp/devdash-supervisor.sock"},
                }
            },
            "stages": [{"id": "robot", "workloads": ["container"]}],
            "dependencies": {},
        }
    )
    backend = Backends(config)
    engine = Engine(config, backend)
    work = config.workloads["container"]

    async def complete():
        run = await asyncio.wait_for(engine.wait(), 30)
        assert run["status"] == "completed", run

    async def observe(predicate):
        async def poll():
            while True:
                value = await backend.observe(work)
                if predicate(value):
                    return value
                await asyncio.sleep(0.1)

        return await asyncio.wait_for(poll(), 25)

    def child(value, logical):
        return next(item for item in value["children"] if item["id"] == logical)

    try:
        engine.launch("robot", True, "start")
        await complete()
        initial = await observe(
            lambda value: (
                value["health"] == "healthy"
                and any(
                    c["id"] == "worker:worker" and c["state"] == "RUNNING"
                    for c in value["children"]
                )
            )
        )
        assert initial["health"] == "healthy" and initial["canonical_unit"] == initial_id
        worker, idle = child(initial, "worker:worker"), child(initial, "idle:idle")
        assert idle["pid"] is None and "start" in idle["actions"]
        raw = next(c for c in initial["children"] if c["kind"] == "process")
        assert raw["actions"] == []
        engine.launch_child("container", raw["id"], raw["epoch"], "stop")
        assert (await engine.wait())["status"] == "failed"  # No raw PID lifecycle path.
        engine.launch_child("container", worker["id"], worker["epoch"], "stop")
        await complete()
        stopped = await observe(lambda value: child(value, worker["id"])["state"] == "STOPPED")
        assert stopped["state"] == "active" and stopped["health"] == "healthy"
        assert child(stopped, worker["id"])["epoch"] == worker["epoch"]
        assert child(stopped, worker["id"])["pid"] is None
        engine.launch_child("container", idle["id"], idle["epoch"], "start")
        await complete()
        running = await observe(lambda value: child(value, idle["id"])["pid"] is not None)
        pid = child(running, idle["id"])["pid"]
        engine.launch_child("container", idle["id"], idle["epoch"], "restart")
        await complete()
        await observe(lambda value: child(value, idle["id"])["pid"] not in (None, pid))
        assert "devdash isolated supervisor worker ready" in await backend.logs(work)
        engine.launch("container", False, "restart")
        await complete()
        restarted = await observe(
            lambda value: any(
                c["id"] == worker["id"] and c["state"] == "RUNNING" for c in value["children"]
            )
        )
        assert child(restarted, worker["id"])["epoch"] != worker["epoch"]
        with pytest.raises(ValueError, match="changed"):
            await backend.operate_child(work, worker["id"], worker["epoch"], "stop")
        engine.launch("container", False, "stop")
        await complete()
        await docker("rm", name)
        replacement_id = await create()
        assert replacement_id != initial_id
        engine.launch("container", False, "start")
        await complete()
        recreated = await observe(
            lambda value: any(
                c["id"] == worker["id"] and c["state"] == "RUNNING" for c in value["children"]
            )
        )
        assert recreated["canonical_unit"] == replacement_id  # Same stable Compose selector.
        assert child(recreated, worker["id"])["epoch"] != worker["epoch"]
    finally:
        await backend.close()
        await docker("rm", "-f", name)


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("DEVDASH_DOCKER_TEST") != "1" or os.environ.get("DEVDASH_SYSTEMD_TEST") != "1",
    reason="requires both dedicated Docker fixture and user manager",
)
async def test_systemd_managed_container_observation_and_child_control(monkeypatch):
    """Only a unique temporary unit/container; no host service configuration changes."""
    import shutil
    from pathlib import Path

    from devdash.adapters.docker import DockerAPI

    name = "devdash-py-it-" + uuid.uuid4().hex[:10]
    docker_path = shutil.which("docker")
    unit = name + ".service"
    runtime = Path(os.environ["XDG_RUNTIME_DIR"]) / "systemd/user"
    runtime.mkdir(parents=True, exist_ok=True)
    path = runtime / unit
    config = Config(
        {
            "workloads": {
                "robot": {
                    "backend": "systemd",
                    "unit": unit,
                    "observer": {"docker": {"container": name}},
                    "controller": {"kind": "supervisord", "socket": "/tmp/devdash-supervisor.sock"},
                }
            },
            "stages": [{"id": "robot", "workloads": ["robot"]}],
            "dependencies": {},
        }
    )
    backend = Backends(config)
    engine = Engine(config, backend)
    work = config.workloads["robot"]

    async def docker(*args):
        proc = await asyncio.create_subprocess_exec(
            docker_path, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        output, error = await asyncio.wait_for(proc.communicate(), 30)
        assert proc.returncode == 0, error.decode()
        return output.decode().strip()

    async def forbidden(*args):
        raise AssertionError("Systemd Workload must not issue Docker Lifecycle calls")

    monkeypatch.setattr(DockerAPI, "operate", forbidden)
    await docker(
        "create",
        "--name",
        name,
        "--network",
        "none",
        "--memory",
        "512m",
        os.environ.get("DEVDASH_TEST_IMAGE", "devdash-supervisor-test:4.3.0"),
    )
    try:
        path.write_text(
            "[Unit]\nDescription=devdash isolated container observer fixture\n[Service]\nType=exec\n"
            f"ExecStart={docker_path} start --attach {name}\n"
            f"ExecStop={docker_path} stop --time 2 {name}\nTimeoutStopSec=10\n"
        )
        await backend.start()
        await backend.systemd.session("user").call("Reload")
        engine.launch("robot", True, "start")
        assert (await engine.wait())["status"] == "completed"

        async def healthy():
            while (await backend.observe(work))["health"] != "healthy":
                await asyncio.sleep(0.1)

        await asyncio.wait_for(healthy(), 20)
        value = await backend.observe(work)
        assert value["state"] == "active" and value["internal"]["state"] == "active"
        assert value["health"] == "healthy" and value["readiness"]["state"] == "ready"
        assert (await engine.wait())["status"] == "completed"
        worker = next(c for c in value["children"] if c["id"] == "worker:worker")
        await backend.operate_child(work, worker["id"], worker["epoch"], "stop")
        value = await backend.observe(work)
        assert value["state"] == "active" and value["health"] == "healthy"
        assert next(c for c in value["children"] if c["id"] == worker["id"])["state"] == "STOPPED"
        # Losing/restarting Core does not send a lifecycle operation to either manager.
        await backend.close()
        backend = Backends(config)
        await backend.start()
        value = await backend.observe(work)
        assert value["state"] == "active" and value["internal"]["state"] == "active"
        await backend.operate(work, "stop")
        value = await backend.observe(work)
        assert value["state"] == "inactive" and value["internal"]["state"] == "inactive"
        assert value["readiness"]["state"] == "not_ready"
    finally:
        if engine.busy:
            engine.task.cancel()
            await asyncio.gather(engine.task, return_exceptions=True)
        # External cleanup belongs only to this temporary test unit.
        proc = await asyncio.create_subprocess_exec("systemctl", "--user", "stop", unit)
        await proc.wait()
        path.unlink(missing_ok=True)
        proc = await asyncio.create_subprocess_exec("systemctl", "--user", "daemon-reload")
        await proc.wait()
        await backend.close()
        await docker("rm", "-f", name)
