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
from devdash.presentation import group_progress, workload_summary


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
            "groups": {
                "robot": {
                    "steps": [{"workload": "robot"}, {"gate": "health"}, {"gate": "continue"}]
                }
            },
            "gates": {
                "health": {
                    "type": "container_ready",
                    "workload": "robot",
                    "timeout_ms": 100,
                    "interval_ms": 5,
                },
                "continue": {"type": "manual"},
            },
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
    backend.systemd.dependencies = AsyncMock(return_value=Graph())
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
    backend.systemd.operate.assert_awaited_once_with(work, action, lease)
    result = await backend.observe(work)
    assert result["state"] == "active" and result["pid"] == 55 and result["memory_bytes"] == 88
    assert result["health"] == "unhealthy" and result["readiness"]["state"] == "not_ready"
    assert result["internal"]["state"] == "active" and result["internal"]["pid"] == 123
    assert result["internal"]["memory_bytes"] == 321
    assert result["children"][0]["id"] == "robot:worker"
    assert result["children"][1]["actions"] == []
    assert not await backend.readiness(work)
    info["State"]["Health"]["Status"] = "healthy"
    assert await backend.readiness(work)
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
    assert restarted.run["status"] == "" and restarted.run["plan"] == []
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


async def test_readiness_and_approval_are_independent_and_restart_preserves_approval(observed):
    from conftest import until

    config, backend, work, info, failure, requests = observed
    engine = Engine(config, backend)
    info["State"]["Health"]["Status"] = "healthy"
    engine.launch("robot", True, "start")
    await until(lambda: engine.run["waiting_gate"] == "continue")
    text = group_progress(engine.snapshot(), "robot")
    assert "[completed] start · workload" in text
    assert "WAITING FOR CONTINUE" in text
    engine.approve("continue")
    assert (await engine.wait())["status"] == "completed"
    engine.launch("robot", True, "restart")
    assert (await engine.wait())["status"] == "completed"
    assert "continue" in engine.approved
    assert [call.args[1] for call in backend.systemd.operate.await_args_list] == [
        "start",
        "stop",
        "start",
    ]
    engine.launch("robot", True, "reinitialize")
    await until(lambda: engine.run["waiting_gate"] == "continue")
    assert "continue" not in engine.approved
    engine.request_cancel()
    assert (await engine.wait())["status"] == "cancelled"
    # Individual operation bypasses the unapproved Gate and unhealthy Docker.
    info["State"]["Health"]["Status"] = "unhealthy"
    engine.launch("robot", False, "restart")
    assert (await engine.wait())["status"] == "completed"
    assert "not executed" in group_progress(engine.snapshot(), "robot")
    assert all(method == "GET" for method, _ in requests)


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


async def test_systemd_gate_inspection_is_preserved_with_docker_observer(observed):
    from devdash.model import UnitKey

    config, backend, _, _, _, _ = observed
    raw = config.json()
    raw["workloads"]["after"] = {"backend": "systemd", "scope": "system", "unit": "after.service"}
    raw["groups"]["robot"]["steps"].append({"workload": "after"})
    config = Config(raw)
    source, target = (
        UnitKey("system", "robot-container.service"),
        UnitKey("system", "after.service"),
    )
    backend.systemd.dependencies.return_value = Graph(
        edges={(source, "Requires", target)}, inspected={source, target}
    )
    engine = Engine(config, backend)
    report = await engine.inspect("robot", "start")
    assert any(d["code"] == "gate_bypass" for d in report["diagnostics"])
    engine.launch("robot", True, "start")
    assert (await engine.wait())["status"] == "failed"
    backend.systemd.operate.assert_not_awaited()


def test_group_preview_never_confuses_active_services_with_completed_steps(config):
    from conftest import Fake

    engine = Engine(config, Fake())
    engine.observations = {key: observation(state="active") for key in config.workloads}
    text = group_progress(engine.snapshot(), "robot")
    assert "not executed" in text and "completed" not in text
    assert "Approval: ready" in text


async def test_progress_distinguishes_workload_and_gate_with_same_logical_id():
    from conftest import Fake, until

    config = Config(
        {
            "workloads": {"same": {"backend": "systemd", "unit": "ordinary.service"}},
            "groups": {"group": {"steps": [{"workload": "same"}, {"gate": "same"}]}},
            "gates": {"same": {"type": "manual"}},
        }
    )
    engine = Engine(config, Fake())
    engine.launch("group", True, "start")
    try:
        await until(lambda: engine.run["waiting_gate"] == "same")
        text = group_progress(engine.snapshot(), "group")
        assert "[completed] start · workload · same" in text
        assert "[WAITING FOR CONTINUE] start · gate · Approval: same" in text
    finally:
        engine.request_cancel()
        await engine.wait()


async def test_rejected_preflight_shows_steps_not_executed(config):
    from conftest import Fake

    backend = Fake()
    backend.graph.stale = True
    engine = Engine(config, backend)
    engine.launch("robot", True, "start")
    assert (await engine.wait())["status"] == "failed"
    text = group_progress(engine.snapshot(), "robot")
    assert "not executed" in text and "pending" not in text and "NEXT" not in text
    assert not backend.calls


async def test_partial_restart_is_not_presented_as_whole_parent_group_result(config):
    from conftest import Fake

    engine = Engine(config, Fake())
    engine.launch("perception", True, "restart")
    assert (await engine.wait())["status"] == "completed"
    parent = group_progress(engine.snapshot(), "robot")
    assert "parallel: camera, imu" in parent and "Approval: ready" in parent and "planner" in parent
    assert "[completed]" not in parent and "not executed" in parent
    assert "Current/last operation: perception restart" in parent
    child = group_progress(engine.snapshot(), "perception")
    assert "[completed] stop" in child and "[completed] start" in child
