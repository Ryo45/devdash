import asyncio
import os
import uuid

import pytest

from devdash.adapters.docker import demultiplex, ready
from devdash.backend import Backends
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
async def test_docker_supervisor_stopped_identity_capabilities_and_epoch():
    name = "devdash-py-it-" + uuid.uuid4().hex[:10]

    async def docker(*args):
        proc = await asyncio.create_subprocess_exec(
            "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        assert proc.returncode == 0, err.decode()
        return out.decode().strip()

    await docker("run", "-d", "--name", name, "--network", "none", "devdash-supervisor-test:4.3.0")
    config = Config(
        {
            "workloads": {
                "container": {
                    "backend": "docker",
                    "docker": {"container": name},
                    "controller": {"kind": "supervisord", "socket": "/tmp/devdash-supervisor.sock"},
                }
            }
        }
    )
    backend = Backends(config)
    work = config.workloads["container"]
    try:
        observation = None
        for _ in range(30):
            observation = await backend.observe(work)
            services = [child for child in observation["children"] if child["kind"] == "service"]
            if any(child["state"] == "RUNNING" for child in services):
                break
            await asyncio.sleep(0.2)
        assert any(child["state"] == "RUNNING" for child in services), observation["children_error"]
        child = next(child for child in services if child["state"] == "RUNNING")
        assert any(
            item["kind"] == "process" and not item["actions"] for item in observation["children"]
        )
        await backend.operate_child(work, child["id"], child["epoch"], "stop")
        observation = await backend.observe(work)
        stopped = next(item for item in observation["children"] if item["id"] == child["id"])
        assert stopped["state"] == "STOPPED" and stopped["pid"] is None
        assert stopped["epoch"] == child["epoch"] and "start" in stopped["actions"]
        for _ in range(30):
            if await backend.readiness(work):
                break
            await asyncio.sleep(0.2)
        assert await backend.readiness(work)  # Child stop does not redefine container Health.
        await backend.operate_child(work, child["id"], child["epoch"], "start")
        await backend.operate(work, "restart")
        with pytest.raises(ValueError, match="changed"):
            await backend.operate_child(work, child["id"], child["epoch"], "stop")
    finally:
        await backend.close()
        await docker("rm", "-f", name)
