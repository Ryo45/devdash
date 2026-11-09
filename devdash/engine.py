"""Core facade: compose independent observation, one Operator and temporary Plans."""

import asyncio
import copy
import uuid

from .observer import Observer
from .operator import Operator
from .plans import PlanExecutor
from .protocol import core_identity


class Engine:
    def __init__(self, config, backend):
        self.backend = backend
        self.core = core_identity(uuid.uuid4().hex)
        self.config_revision = 0
        self.observer = Observer(config, backend)
        self.operator = Operator(config, backend, self.observer.inspect)
        self.plans = PlanExecutor(self.operator, self.observer, lambda: self.config_revision)
        backend.emit = self.on_backend_event

    @property
    def config(self):
        return self.operator.config

    @config.setter
    def config(self, value):
        self.operator.config = self.observer.config = value

    @property
    def busy(self):
        return self.operator.busy

    @property
    def task(self):
        return self.operator.task

    @property
    def run(self):
        return self.operator.run

    @property
    def cancel(self):
        return self.operator.cancel

    # Read-only facade views preserve the existing embedding API, with one owner each.
    @property
    def events(self):
        return self.observer.events

    @property
    def known(self):
        return self.observer.known

    @property
    def outages(self):
        return self.observer.outages

    @property
    def reports(self):
        return self.observer.reports

    @property
    def monitor_gap(self):
        return self.observer.monitor_gap

    @property
    def epoch(self):
        return self.observer.epoch

    @property
    def observations(self):
        return self.observer.observations

    @observations.setter
    def observations(self, value):
        self.observer.observations = value

    def emit(self, *args, **kwargs):
        return self.observer.emit(*args, **kwargs)

    def invalidate(self):
        self.observer.invalidate()

    def on_backend_event(self, event):
        record = (
            self.observer.on_backend_event(event) if event["kind"] != "operation_prepared" else None
        )
        self.operator.on_backend_event(event)
        self.plans.on_backend_event(event)
        if (
            record
            and self.busy
            and (
                event["kind"] in ("reloading", "reloaded")
                or event["kind"] == "job"
                and event.get("correlation") != "requested"
                or self.operator.disturbances
            )
        ):
            record["level"] = "warning"

    def launch(self, target, stage, action):
        self.operator.launch(target, stage, action)

    def launch_next(self, after):
        self.operator.launch_next(after)

    def launch_child(self, *args):
        self.operator.launch_child(*args)

    def request_cancel(self):
        self.operator.request_cancel()
        self.plans.wake()

    async def wait(self):
        return await self.operator.wait()

    async def inspect(self, *args):
        return await self.observer.inspect(*args)

    def update(self, values):
        self.observer.update(values)
        self.plans.observations_changed()
        if self.busy:
            # Observation is supporting evidence, never the operation success source.
            for event in self.events:
                if event["kind"] == "state_changed" and event["id"] == self.observer.sequence:
                    event["level"] = "warning"

    async def poll(self):
        await self.observer.poll()
        self.plans.observations_changed()

    async def monitor(self):
        async def watch():
            async for _ in self.observer.watch():
                self.plans.observations_changed()

        await asyncio.gather(watch(), self.observer.diagnostics())

    def snapshot(self):
        return copy.deepcopy(
            dict(
                core=self.core,
                config=self.config.json(),
                config_revision=self.config_revision,
                run=self.run,
                plan=self.plans.snapshot(),
                observations=self.observer.current(),
                inspections=self.observer.inspection_snapshot(),
                events=list(self.events),
                monitoring=self.backend.health(),
                monitor_gap=self.monitor_gap,
            )
        )
