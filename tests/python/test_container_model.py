"""Container tests without Docker, ROS, or a running supervisor."""

import asyncio

import pytest
from conftest import Fake, until

from devdash.adapters.docker import DockerAPI, demultiplex, epoch, ready, supervisor_children
from devdash.backend import Backends
from devdash.engine import Engine
from devdash.model import Config, ConfigError, systemd_key

CONFIG = """
workloads:
  robot:
    backend: docker
    docker: {container: robot-dev}
groups:
  robot:
    steps: [{workload: robot}, {gate: ready}]
gates:
  ready: {type: container_ready, workload: robot, timeout_ms: 80, interval_ms: 5}
"""


@pytest.mark.parametrize(
    "replacement",
    [
        ("{container: robot-dev}", "{}"),
        (
            "{container: robot-dev}",
            "{container: robot-dev, compose: {project: robot, service: camera}}",
        ),
        ("{workload: robot}", '{workload: "worker:worker"}'),
        ("workload: robot, timeout_ms", "workload: missing, timeout_ms"),
    ],
)
def test_container_reference_validation_and_children_cannot_be_steps(replacement):
    assert systemd_key(Config.parse(CONFIG).workloads["robot"]) is None
    with pytest.raises(ConfigError):
        Config.parse(CONFIG.replace(*replacement))


@pytest.mark.parametrize("health", [None, "healthy", "unhealthy"])
async def test_container_gate_is_independent_of_systemd_and_delegates_health(health):
    config, backend = Config.parse(CONFIG), Fake()
    backend.lost.set()  # systemd unavailable; this must not affect Docker.

    async def operate(work, action, lease=None):
        assert lease is None
        backend.calls.append(("parent", action))

    async def readiness(work):
        return ready(
            {"State": {"Running": True, **({"Health": {"Status": health}} if health else {})}}
        )

    backend.operate, backend.readiness = operate, readiness
    engine = Engine(config, backend)
    report = await engine.inspect("robot", "start")
    assert not report["requested_units"] and any(
        "Docker lifecycle" in s for s in report["incomplete"]
    )
    backend.revision += 1
    assert not engine.snapshot()["inspections"]["robot"]["stale"]
    engine.launch("robot", True, "start")
    assert (await asyncio.wait_for(engine.wait(), 2))["status"] == (
        "failed" if health == "unhealthy" else "completed"
    )
    assert backend.calls == [("parent", "start")]  # No rollback, even after timeout.
    backend.calls.clear()
    engine.launch("robot", False, "start")
    assert (await engine.wait())["status"] == "completed"
    assert backend.calls == [("parent", "start")]  # Direct operation bypasses health.


async def test_child_operation_uses_global_busy_and_cancel_drains_issued_operation():
    backend = Fake()
    release = asyncio.Event()

    async def operate_child(work, child, epoch, action):
        backend.calls.append((child, action))
        await release.wait()

    backend.operate_child = operate_child
    engine = Engine(Config.parse(CONFIG), backend)
    engine.launch_child("robot", "worker:worker", "epoch", "restart")
    await until(lambda: bool(backend.calls))
    with pytest.raises(ValueError, match="Another"):
        engine.launch("robot", True, "stop")
    with pytest.raises(ValueError, match="Another"):
        engine.launch_child("robot", "other", "epoch", "stop")
    engine.request_cancel()
    assert engine.run["status"] == "cancelling" and not engine.task.done()
    release.set()
    run = await asyncio.wait_for(engine.wait(), 2)
    assert run["status"] == "cancelled" and run["progress"][0]["status"] == "completed"
    assert backend.calls == [("worker:worker", "restart")]
    with pytest.raises(ValueError, match="Invalid"):
        engine.launch_child("robot", "worker:worker", "epoch", "reinitialize")


async def test_missing_docker_socket_reports_access_context(tmp_path):
    config = Config.parse(CONFIG)
    config.workloads["robot"]["docker"]["socket"] = str(tmp_path / "missing.sock")
    backend = Backends(config)
    try:
        with pytest.raises(RuntimeError, match="Docker socket.*check permissions"):
            await backend.observe(config.workloads["robot"])
        assert not backend.apis
    finally:
        await backend.close()


@pytest.mark.parametrize(
    "minimum,maximum,expected",
    [
        ("1.44", "1.52", "v1.44"),
        ("1.24", "1.43", "v1.41"),
        ("1.53", "1.53", None),
        ("1.24", "1.40", None),
    ],
)
async def test_api_negotiation_rejects_unsupported_server(monkeypatch, minimum, maximum, expected):
    async def request(self, *args, **kwargs):
        return 200, {"MinAPIVersion": minimum, "ApiVersion": maximum}

    monkeypatch.setattr(DockerAPI, "request", request)
    api = DockerAPI("/unused")
    try:
        if expected:
            assert (await api.connect()).version == expected
        else:
            with pytest.raises(RuntimeError, match="outside supported"):
                await api.connect()
            assert api.client.closed
    finally:
        await api.close()


@pytest.mark.parametrize("data", [b"\x01", b"\x01\0\0\0\0\0\0\x02x", b"\x03\0\0\0\0\0\0\x01x"])
def test_multiplexed_errors(data):
    with pytest.raises(ValueError):
        demultiplex(data)
    assert demultiplex(b"\x01\0\0\0\0\0\0\x01x\x02\0\0\0\0\0\0\x01y") == b"xy"


def test_stopped_service_identity_and_container_health_are_separate():
    info = {
        "Id": "container",
        "State": {"Running": True, "StartedAt": "now", "Health": {"Status": "healthy"}},
    }
    service = supervisor_children(
        epoch(info),
        {
            "epoch": "owner",
            "processes": [
                {
                    "group": "robot",
                    "name": "camera",
                    "statename": "STOPPED",
                    "pid": 0,
                    "exitstatus": 0,
                }
            ],
        },
    )[0]
    assert service["id"] == "robot:camera" and service["pid"] is None
    assert service["actions"] == ["start", "restart"]
    assert service["memory_bytes"] is None and service["cpu_percent"] is None
    assert ready(info)
    info["State"]["Restarting"] = True
    assert not ready(info)


async def test_top_resources_are_observation_only(monkeypatch):
    async def request(self, method, path, **kwargs):
        if path[-1] == "stats":
            return 200, {}
        return 200, {
            "Titles": ["PID", "COMMAND", "RSS", "%CPU", "STAT"],
            "Processes": [["42", "sleep", "100", "2.5", "S"]],
        }

    monkeypatch.setattr(DockerAPI, "request", request)
    api = DockerAPI("/unused")
    try:
        result = await api.observe(
            {},
            {
                "Id": "container",
                "State": {"Running": True, "Status": "running", "StartedAt": "now"},
            },
        )
        child = result["children"][0]
        assert child["pid"] == 42 and child["pid_namespace"] == "Docker daemon host"
        assert child["memory_bytes"] == 102400 and child["cpu_percent"] == 2.5
        assert child["actions"] == [] and child["kind"] == "process"
    finally:
        await api.close()
