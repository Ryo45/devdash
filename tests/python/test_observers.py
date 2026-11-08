"""Lifecycle routing and internal observation are independent, without a real daemon."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from devdash.adapters.docker import DockerAPI
from devdash.backend import Backends
from devdash.engine import Engine
from devdash.inspection import Graph
from devdash.model import Config, ConfigError, observation
from devdash.presentation import stage_progress, workload_summary


def observed_config():
    return Config(
        {
            "workloads": {
                "robot": {
                    "backend": "systemd",
                    "scope": "system",
                    "unit": "robot-container.service",
                    "observer": {"docker": {"container": "robot"}},
                    "controller": {"kind": "supervisord", "socket": "/run/supervisor.sock"},
                }
            },
            "stages": [{"id": "robot", "workloads": ["robot"]}],
            "dependencies": {},
        }
    )


@pytest.fixture
async def observed(monkeypatch):
    config = observed_config()
    backend = Backends(config)
    work = config.workloads["robot"]
    info = {
        "Id": "container-1",
        "State": {
            "Running": True,
            "Status": "running",
            "StartedAt": "now",
            "Pid": 123,
            "Health": {"Status": "unhealthy"},
        },
    }
    requests = []
    failure = {"error": None}

    async def request(self, method, path, **kwargs):
        requests.append((method, path))
        assert method == "GET", "Observer must not send Lifecycle mutations"
        if path == ["version"]:
            return 200, {"ApiVersion": "1.45"}
        if failure["error"]:
            raise failure["error"]
        if path[-1] == "json":
            return 200, deepcopy(info)
        if path[-1] == "stats":
            return 200, {"memory_stats": {"usage": 321}}
        if path[-1] == "top":
            return 200, {"Titles": ["PID", "COMMAND"], "Processes": [["123", "worker"]]}
        raise AssertionError(path)

    async def rpc(self, container, controller, request):
        assert container["Id"] == info["Id"]
        return {
            "epoch": "supervisor-1",
            "processes": [
                {
                    "name": "worker",
                    "group": "robot",
                    "statename": "RUNNING",
                    "pid": 2,
                    "exitstatus": 0,
                }
            ],
        }

    monkeypatch.setattr(DockerAPI, "request", request)
    monkeypatch.setattr(DockerAPI, "rpc", rpc)
    backend.systemd.observe = AsyncMock(
        return_value=observation(state="active", pid=55, memory_bytes=88)
    )
    backend.systemd.operate = AsyncMock()
    from devdash.model import UnitKey

    backend.systemd.dependencies = AsyncMock(
        return_value=Graph(inspected={UnitKey("system", "robot-container.service")})
    )
    backend.systemd.lease = lambda scope: asyncio.Event()
    try:
        yield config, backend, work, info, failure, requests
    finally:
        await backend.close()


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
async def test_systemd_lifecycle_docker_health_and_children_have_one_operation_path(
    observed, action
):
    config, backend, work, info, failure, requests = observed
    lease = asyncio.Event()
    await backend.operate(work, action, lease)
    backend.systemd.operate.assert_awaited_once_with(work, action, lease, None)
    result = await backend.observe(work)
    assert result["state"] == "active" and result["pid"] == 55 and result["memory_bytes"] == 88
    assert result["health"] == "unhealthy" and result["readiness"]["state"] == "not_ready"
    assert result["internal"]["state"] == "active" and result["internal"]["pid"] == 123
    assert result["internal"]["memory_bytes"] == 321
    assert result["children"][0]["id"] == "robot:worker"
    assert result["children"][1]["actions"] == []
    assert (await backend.observe(work))["readiness"]["state"] == "not_ready"
    info["State"]["Health"]["Status"] = "healthy"
    assert (await backend.observe(work))["readiness"]["state"] == "ready"
    assert all(method == "GET" for method, _ in requests)


async def test_internal_child_operations_use_supervisor_in_observed_container(
    observed, monkeypatch
):
    _, backend, work, _, _, _ = observed
    child = next(c for c in (await backend.observe(work))["children"] if c["kind"] == "service")
    original = DockerAPI.rpc
    operations = []

    async def rpc(self, info, controller, request):
        if request["command"] == "operate":
            operations.append(request)
            return {}
        return await original(self, info, controller, request)

    monkeypatch.setattr(DockerAPI, "rpc", rpc)
    await backend.operate_child(work, child["id"], child["epoch"], "restart")
    assert operations == [
        {"command": "operate", "id": "robot:worker", "epoch": "supervisor-1", "action": "restart"}
    ]
    backend.systemd.operate.assert_not_awaited()
    with pytest.raises(ValueError, match="raw PIDs"):
        await backend.operate_child(work, "process:123", child["epoch"], "stop")


async def test_observer_disconnect_recovery_and_core_restart_do_not_infer_or_repair(observed):
    config, backend, work, info, failure, _ = observed
    engine = Engine(config, backend)
    await engine.poll()
    assert not engine.events
    failure["error"] = OSError("Docker disconnected")
    await engine.poll()
    value = engine.observations["robot"]
    assert value["state"] == "active" and value["internal"]["state"] == "unknown"
    assert value["health"] is None and value["readiness"]["state"] == "unknown"
    assert value["children"] == [] and "Docker disconnected" in value["observer_error"]
    assert [event["kind"] for event in engine.events] == ["observer_lost"]
    await engine.poll()
    assert len(engine.events) == 1
    info["Id"] = "container-recreated-during-gap"
    info["State"]["Health"]["Status"] = "healthy"
    failure["error"] = None
    await engine.poll()
    assert [event["kind"] for event in engine.events] == ["observer_lost", "observer_restored"]
    assert engine.observations["robot"]["readiness"]["state"] == "ready"
    restarted = Engine(config, backend)
    assert not restarted.observations
    assert restarted.run["status"] == "" and restarted.run["progress"] == []
    await restarted.poll()
    assert not restarted.events and restarted.run["status"] == ""
    backend.systemd.operate.assert_not_awaited()


async def test_systemd_loss_does_not_hide_independent_docker_observation(observed):
    _, backend, work, _, _, _ = observed
    backend.systemd.observe.side_effect = OSError("system bus disconnected")
    result = await backend.observe(work)
    assert result["state"] == "unknown" and "system bus" in result["error"]
    assert result["internal"]["state"] == "active" and result["health"] == "unhealthy"


async def test_observer_timeout_keeps_lifecycle_state(observed, monkeypatch):
    _, backend, work, _, _, _ = observed
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    original = asyncio.wait_for

    async def short_timeout(awaitable, timeout):
        return await original(awaitable, 0.02)

    async def blocked(work):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(backend, "observe_internal", blocked)
    monkeypatch.setattr("devdash.backend.asyncio.wait_for", short_timeout)
    result = await backend.observe(work)
    assert entered.is_set() and cancelled.is_set()
    assert result["state"] == "active" and result["readiness"]["state"] == "unknown"
    assert result["internal"]["state"] == "unknown"
    assert result["observer_error"] is not None


@pytest.mark.parametrize(
    "replacement",
    [None, {}, {"docker": {}}, {"bridge": {}}, {"docker": {"container": "robot", "unit": "bad"}}],
)
def test_observer_configuration_is_explicit_and_validated(replacement):
    raw = observed_config().json()
    raw["workloads"]["robot"]["observer"] = replacement
    with pytest.raises(ConfigError):
        Config(raw)


@pytest.mark.parametrize("direct_first", [True, False])
def test_same_container_cannot_have_both_systemd_and_direct_docker_lifecycle(direct_first):
    raw = observed_config().json()
    direct = {"direct": {"backend": "docker", "docker": {"container": "robot"}}}
    raw["workloads"] = direct | raw["workloads"] if direct_first else raw["workloads"] | direct
    with pytest.raises(ConfigError, match="Duplicate"):
        Config(raw)


async def test_observer_health_does_not_block_core_start(observed):
    config, backend, work, info, failure, requests = observed
    engine = Engine(config, backend)
    assert info["State"]["Health"]["Status"] == "unhealthy"
    engine.launch("robot", True, "start")
    assert (await engine.wait())["status"] == "completed"
    engine.launch("robot", True, "restart")
    assert (await engine.wait())["status"] == "completed"
    assert [call.args[1] for call in backend.systemd.operate.await_args_list] == [
        "start",
        "restart",
    ]
    assert not hasattr(engine, "approved")


async def test_presentation_reports_facts_roles_and_staleness(observed):
    _, backend, work, _, _, _ = observed
    value = await backend.observe(work)
    summary = workload_summary(work, value)
    for text in (
        "Execution: active",
        "Readiness: not_ready",
        "Health: unhealthy",
        "systemd (system)",
        "Docker API",
        "supervisord (container)",
    ):
        assert text in summary
    assert "STALE" in workload_summary(work, value, connected=False)
    assert "Execution: unknown" in workload_summary(work, {})


@pytest.mark.parametrize(
    "service_type,state,expected",
    [
        ("notify", "active", "ready"),
        ("notify-reload", "active", "ready"),
        ("notify", "activating", "not_ready"),
        ("notify", "inactive", "not_ready"),
        ("simple", "active", "unknown"),
        ("exec", "active", "unknown"),
    ],
)
async def test_systemd_notify_readiness_is_not_inferred_for_simple_services(
    service_type, state, expected
):
    from types import SimpleNamespace

    from devdash.adapters.systemd import Systemd

    config = Config({"workloads": {"service": {"backend": "systemd", "unit": "ordinary.service"}}})
    systemd = Systemd(config)
    session = SimpleNamespace(
        generation=1,
        check=lambda: None,
        unit=AsyncMock(return_value=("/unit", {"LoadState": "loaded", "ActiveState": state})),
        properties=AsyncMock(return_value={"Type": service_type}),
    )
    systemd.session = lambda scope: session
    result = await systemd.observe(config.workloads["service"])
    assert result["state"] == state and result["readiness"]["state"] == expected


async def test_later_stage_pull_in_is_detected_with_docker_observer(observed):
    from devdash.model import UnitKey

    config, backend, _, _, _, _ = observed
    raw = config.json()
    raw["workloads"]["after"] = dict(backend="systemd", scope="system", unit="after.service")
    raw["stages"].append(dict(id="after", workloads=["after"]))
    config = Config(raw)
    source, target = (
        UnitKey("system", "robot-container.service"),
        UnitKey("system", "after.service"),
    )
    backend.systemd.dependencies.return_value = Graph(
        edges={(source, "Requires", target)}, inspected={source, target}
    )
    engine = Engine(config, backend)
    engine.launch("robot", True, "start")
    assert (await engine.wait())["status"] == "failed"
    backend.systemd.operate.assert_not_awaited()


def test_stage_preview_never_confuses_active_services_with_completed_operations(config):
    from conftest import Fake

    engine = Engine(config, Fake())
    engine.observations = {key: observation(state="active") for key in config.workloads}
    assert "not requested" in stage_progress(engine.snapshot(), "sensors")


async def test_stage_and_workload_can_share_id_without_progress_confusion():
    from conftest import Fake

    config = Config(
        dict(
            workloads={"same": dict(backend="systemd", unit="same.service")},
            stages=[dict(id="same", workloads=["same"])],
        )
    )
    engine = Engine(config, Fake())
    engine.launch("same", False, "start")
    await engine.wait()
    assert "not requested" in stage_progress(engine.snapshot(), "same")


async def test_blocked_stage_reports_unissued_workloads(config):
    from conftest import Fake

    backend = Fake()
    backend.graph.stale = True
    engine = Engine(config, backend)
    engine.launch("sensors", True, "start")
    assert (await engine.wait())["status"] == "failed"
    assert "[skipped]" in stage_progress(engine.snapshot(), "sensors")
    assert not backend.calls


async def test_other_stage_progress_is_not_claimed_after_partial_restart(config):
    from conftest import Fake

    engine = Engine(config, Fake())
    engine.launch("perception", True, "restart")
    await engine.wait()
    assert "not requested" in stage_progress(engine.snapshot(), "sensors")
    assert "[completed]" in stage_progress(engine.snapshot(), "perception")


async def test_http_is_observation_only_including_error_and_disconnect(monkeypatch):
    from aiohttp import web

    state = {"status": 204}

    async def endpoint(request):
        return web.Response(status=state["status"])

    app = web.Application()
    app.router.add_get("/health", endpoint)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    config = Config(
        dict(
            workloads={
                "api": dict(
                    backend="systemd",
                    unit="api.service",
                    observer={
                        "http": dict(url=f"http://127.0.0.1:{port}/health", expected_status=204)
                    },
                )
            },
            stages=[dict(id="api", workloads=["api"])],
        )
    )
    backend = Backends(config)
    backend.systemd.observe = AsyncMock(return_value=observation(state="active"))
    backend.systemd.operate = AsyncMock()
    try:
        assert (await backend.observe(config.workloads["api"]))["http"]["state"] == "ready"
        state["status"] = 503
        value = await backend.observe(config.workloads["api"])
        assert value["state"] == "active" and value["http"]["state"] == "not_ready"
        await runner.cleanup()
        value = await backend.observe(config.workloads["api"])
        assert value["state"] == "active" and value["http"]["state"] == "unknown"
        backend.systemd.operate.assert_not_awaited()
    finally:
        await runner.cleanup()
        await backend.close()


async def test_cancel_during_docker_resolution_does_not_issue_lifecycle():
    config = Config(
        dict(workloads={"docker": dict(backend="docker", docker={"container": "robot"})})
    )
    backend = Backends(config)
    entered, release = asyncio.Event(), asyncio.Event()
    api = type("API", (), {"operate": AsyncMock()})()

    async def resolve(work):
        entered.set()
        await release.wait()
        return api, {"Id": "container"}

    backend.resolved = resolve
    engine = Engine(config, backend)
    try:
        engine.launch("docker", False, "start")
        await entered.wait()
        engine.request_cancel()
        release.set()
        run = await engine.wait()
        assert run["status"] == "cancelled" and run["progress"][0]["status"] == "skipped"
        api.operate.assert_not_awaited()
    finally:
        await backend.close()


async def test_cancel_during_controller_snapshot_does_not_issue_child_mutation(
    observed, monkeypatch
):
    config, backend, work, _, _, _ = observed
    child = next(c for c in (await backend.observe(work))["children"] if c["kind"] == "service")
    entered, release = asyncio.Event(), asyncio.Event()
    original = DockerAPI.rpc
    requests = []

    async def delayed(self, info, controller, request):
        requests.append(request["command"])
        entered.set()
        await release.wait()
        return await original(self, info, controller, request)

    monkeypatch.setattr(DockerAPI, "rpc", delayed)
    engine = Engine(config, backend)
    engine.launch_child("robot", child["id"], child["epoch"], "restart")
    await entered.wait()
    engine.request_cancel()
    release.set()
    run = await engine.wait()
    assert run["status"] == "cancelled" and run["progress"][0]["status"] == "skipped"
    assert requests == ["snapshot"]
