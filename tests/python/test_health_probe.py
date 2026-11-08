"""The example probe is unit-side and read-only, independent of devdash Core."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location(
    "wait_health",
    Path(__file__).resolve().parents[2] / "examples/systemd-docker-health/wait_health.py",
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def info(health="starting", **state):
    return {
        "Id": "original",
        "State": {"Running": True, "StartedAt": "first", "Health": {"Status": health}, **state},
    }


@pytest.fixture
def clock(monkeypatch):
    current = [0.0]
    monkeypatch.setattr(probe.time, "monotonic", lambda: current[0])
    monkeypatch.setattr(
        probe.time, "sleep", lambda seconds: current.__setitem__(0, current[0] + seconds)
    )
    return current


def test_waits_for_health_and_allows_initial_docker_start_race(monkeypatch, clock):
    inspect = Mock(side_effect=[info(Running=False, Status="created"), info(), info("healthy")])
    monkeypatch.setattr(probe, "inspect_container", inspect)
    probe.wait_health("robot", timeout=3, interval=1)
    assert inspect.call_count == 3 and clock[0] == 2


@pytest.mark.parametrize("health", ["unhealthy", None, "unexpected"])
def test_absent_or_failed_health_is_never_ready(monkeypatch, clock, health):
    monkeypatch.setattr(probe, "inspect_container", Mock(return_value=info(health)))
    with pytest.raises(probe.HealthError, match="Docker Health is"):
        probe.wait_health("robot")


def test_deadline_is_bounded_without_wall_clock_sleep(monkeypatch, clock):
    inspect = Mock(return_value=info())
    monkeypatch.setattr(probe, "inspect_container", inspect)
    with pytest.raises(probe.HealthError, match="deadline"):
        probe.wait_health("robot", timeout=2, interval=1)
    assert clock[0] == 2 and inspect.call_count == 2


@pytest.mark.parametrize(
    "replacement",
    [
        {"Id": "replacement", "State": info()["State"]},
        info("healthy", StartedAt="replacement"),
        info(Running=False, Status="exited"),
    ],
)
def test_replacement_restart_or_exit_during_wait_is_failure(monkeypatch, clock, replacement):
    monkeypatch.setattr(probe, "inspect_container", Mock(side_effect=[info(), replacement]))
    with pytest.raises(probe.HealthError):
        probe.wait_health("robot")


def test_late_inspection_cannot_succeed_after_deadline(monkeypatch, clock):
    def inspect(*args):
        clock[0] = 2
        return info("healthy")

    monkeypatch.setattr(probe, "inspect_container", inspect)
    with pytest.raises(probe.HealthError, match="deadline"):
        probe.wait_health("robot", timeout=1)
