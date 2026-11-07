import asyncio
import os
import uuid

import pytest
from conftest import until

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
            "groups": {
                "robot": {
                    "steps": [{"workload": "container"}, {"gate": "ready"}, {"gate": "operator"}]
                }
            },
            "gates": {
                "ready": {
                    "type": "container_ready",
                    "workload": "container",
                    "timeout_ms": 20000,
                    "interval_ms": 100,
                },
                "operator": {"type": "manual"},
            },
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
        await until(
            lambda: engine.run["waiting_gate"] == "operator" or engine.run["status"] == "failed",
            timeout=25,
        )
        assert engine.run["waiting_gate"] == "operator", engine.run
        initial = await observe(
            lambda value: any(
                c["id"] == "worker:worker" and c["state"] == "RUNNING" for c in value["children"]
            )
        )
        assert initial["health"] == "healthy" and initial["canonical_unit"] == initial_id
        worker, idle = child(initial, "worker:worker"), child(initial, "idle:idle")
        assert idle["pid"] is None and "start" in idle["actions"]
        with pytest.raises(ValueError, match="Another"):
            engine.launch_child("container", idle["id"], idle["epoch"], "start")
        engine.approve("operator")
        await complete()
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
