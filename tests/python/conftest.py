import asyncio
import copy

import pytest

from devdash.engine import Engine
from devdash.inspection import Graph
from devdash.model import Config, observation


@pytest.fixture
def config():
    return Config(
        dict(
            workloads={
                key: dict(backend="systemd", unit=f"{key}.service")
                for key in ("camera", "imu", "detector", "planner")
            },
            gates={"ready": {"type": "manual"}},
            groups={
                "robot": {
                    "steps": [
                        {"group": "sensors"},
                        {"gate": "ready"},
                        {"group": "perception"},
                        {"workload": "planner"},
                    ]
                },
                "sensors": {"steps": [{"parallel": ["camera", "imu"]}]},
                "perception": {"steps": [{"workload": "detector"}]},
            },
        )
    )


class Fake:
    revision = 0

    def __init__(self):
        self.calls = []
        self.fail = set()
        self.block = {}
        self.started = asyncio.Event()
        self.lost = asyncio.Event()
        self.observations = {}
        self.graph = Graph()

    def lease(self, scope):
        return self.lost

    def health(self):
        return {}

    def recent(self, work):
        return False

    async def operate(self, work, action, lease=None):
        key = work["unit"].removesuffix(".service")
        self.calls.append((key, action))
        self.started.set()
        if key in self.block:
            await self.block[key].wait()
        if key in self.fail:
            raise RuntimeError("fake failed: " + key)
        self.observations[key] = observation(state="inactive" if action == "stop" else "active")

    async def observe(self, work):
        return self.observations.get(
            work["unit"].removesuffix(".service"), observation(state="inactive")
        )

    async def dependencies(self, seeds):
        graph = copy.deepcopy(self.graph)
        graph.inspected |= seeds
        return graph

    async def readiness(self, work):
        return True

    async def validate_registration(self, work):
        pass

    async def scan(self, scope):
        return ["importable.service"]

    async def logs(self, work):
        return "fixture log\n"


@pytest.fixture
def backend():
    return Fake()


@pytest.fixture
def engine(config, backend):
    return Engine(config, backend)


async def until(predicate, timeout=2):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout)


@pytest.fixture
async def health_scenario(tmp_path, monkeypatch):
    """Only requested by opt-in dedicated unit/container integration tests."""
    from unittest.mock import AsyncMock

    from health_scenario import HealthScenario

    from devdash.adapters.docker import DockerAPI

    forbidden = AsyncMock(side_effect=AssertionError("Docker Lifecycle bypassed systemd"))
    monkeypatch.setattr(DockerAPI, "operate", forbidden)
    scenarios = []

    async def create(**options):
        scenario = HealthScenario(tmp_path)
        scenarios.append(scenario)
        return await scenario.start(**options)

    try:
        yield create
    finally:
        for scenario in reversed(scenarios):
            await scenario.close()
        forbidden.assert_not_awaited()
