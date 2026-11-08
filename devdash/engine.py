"""One explicitly requested Stage at a time; no workflow, gate, or durable cursor."""

import asyncio
import copy
import time
import uuid
from collections import deque

from .inspection import Graph, inspect
from .model import observation, systemd_key
from .protocol import core_identity

ACTIONS = {"start", "stop", "restart"}


class Engine:
    def __init__(self, config, backend):
        self.config, self.backend = config, backend
        self.core = core_identity(uuid.uuid4().hex)
        self.cancel = asyncio.Event()
        self.task = None
        self.run = dict(target="", kind="", action="", status="", progress=[], error=None)
        self.events = deque(maxlen=200)
        self.sequence = 0
        self.observations, self.known = {}, {}
        self.outages = set()
        self.monitor_gap = None
        self.reports = {}
        self.epoch = 0
        self.config_revision = 0
        self.started = self.finished = 0.0
        backend.emit = self.on_backend_event

    @property
    def busy(self):
        return self.task is not None and not self.task.done()

    def emit(self, level, message, workload=None, kind="notification", **facts):
        self.sequence += 1
        event = dict(
            id=self.sequence,
            level=level,
            workload=workload,
            message=message,
            kind=kind,
            observed_unix_ms=int(time.time() * 1000),
            scope=None,
            job_path=None,
            job_result=None,
            correlation="unknown",
        )
        event.update(facts)
        self.events.append(event)

    def on_backend_event(self, event):
        event = dict(event)
        scope = event.get("scope")
        if scope and not any(
            w["backend"] == "systemd" and w["scope"] == scope
            for w in self.config.workloads.values()
        ):
            return
        workload = event.pop("workload", None)
        message = event.pop("message")
        kind = event.pop("kind")
        observed = event.pop("observed_at", time.monotonic())
        correlated = event.get("correlation") == "requested"
        overlapping = (
            bool(self.started)
            and self.started <= observed
            and (not self.finished or observed <= self.finished)
        )
        level = (
            "warning"
            if kind in ("disconnected", "gap")
            or overlapping
            and (kind in ("reloading", "reloaded") or kind == "job" and not correlated)
            else "info"
        )
        if kind in ("disconnected", "reconnected", "gap"):
            self.monitor_gap = f"{scope or 'Backend'} event history has a gap; current observations form a new baseline"
        self.emit(level, message, workload, kind, **event)

    def invalidate(self):
        self.epoch += 1

    def request_cancel(self):
        if not self.busy:
            raise ValueError("No operation is running")
        self.cancel.set()
        self.run["status"] = "cancelling"

    def launch(self, target, stage, action):
        if action not in ACTIONS:
            raise ValueError("Invalid operation")
        members = self.config.stage(target)["workloads"] if stage else [target]
        if any(key not in self.config.workloads for key in members):
            raise ValueError("Unknown Workload")
        self._launch(
            target,
            "stage" if stage else "workload",
            action,
            lambda: self.execute(target, members, stage, action),
        )

    def launch_next(self, after):
        self.launch(self.config.next_stage(after)["id"], True, "start")

    def launch_child(self, parent, child, epoch, action):
        if parent not in self.config.workloads or action not in ACTIONS:
            raise ValueError("Invalid child operation")

        async def operation():
            if not self.cancel.is_set():
                await self.track(
                    child,
                    action,
                    self.backend.operate_child(
                        self.config.workloads[parent], child, epoch, action, self.cancel
                    ),
                )

        self._launch(f"{parent}/{child}", "child", action, operation)

    def _launch(self, target, kind, action, operation):
        if self.busy:
            raise ValueError("Another operation is running")
        self.cancel = asyncio.Event()
        self.started, self.finished = time.monotonic(), 0.0
        self.run = dict(
            target=target, kind=kind, action=action, status="running", progress=[], error=None
        )

        async def run():
            try:
                await operation()
                self.run["status"] = "cancelled" if self.cancel.is_set() else "completed"
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.run.update(status="failed", error=str(error))
            finally:
                self.finished = time.monotonic()

        self.task = asyncio.create_task(run(), name=f"operation:{target}:{action}")

    async def wait(self):
        if self.task:
            await self.task
        return copy.deepcopy(self.run)

    async def track(self, key, action, operation):
        progress = dict(workload=key, action=action, status="running", error=None)
        self.run["progress"].append(progress)
        try:
            result = await operation
        except Exception as error:
            progress.update(status="failed", error=str(error))
            raise
        else:
            progress["status"] = "skipped" if result is False else "completed"

    async def execute(self, target, members, stage, action):
        scopes = {
            self.config.workloads[key]["scope"]
            for key in members
            if self.config.workloads[key]["backend"] == "systemd"
        }
        leases = {scope: self.backend.lease(scope) for scope in scopes}
        if stage and not self.cancel.is_set():
            report = await self.inspect(target, action)
            if not self.cancel.is_set() and (
                report["stale"]
                or bool(report["uninspected_units"])
                or any(d["level"] == "error" for d in report["diagnostics"])
            ):
                self.run["progress"] = [
                    dict(
                        workload=key,
                        action=action,
                        status="skipped",
                        error="Inspection blocked operation",
                    )
                    for key in members
                ]
                raise RuntimeError(
                    "Dependency inspection blocked operation: "
                    + "; ".join(
                        [d["message"] for d in report["diagnostics"] if d["level"] == "error"]
                        + report["incomplete"]
                    )
                )
        if self.cancel.is_set():
            self.run["progress"] = [
                dict(workload=key, action=action, status="skipped", error=None) for key in members
            ]
            return

        async def workload(key):
            work = self.config.workloads[key]
            return await self.backend.operate(
                work,
                action,
                leases.get(work["scope"]) if work["backend"] == "systemd" else None,
                self.cancel,
            )

        # All members are independent; drain every dispatched result even on failure/cancel.
        results = await asyncio.gather(
            *(self.track(key, action, workload(key)) for key in members), return_exceptions=True
        )
        errors = [str(value) for value in results if isinstance(value, BaseException)]
        if errors:
            raise RuntimeError("; ".join(errors))

    async def inspect(self, stage, action):
        config = self.config
        epoch = self.epoch
        seeds = {
            key
            for logical in config.stage(stage)["workloads"]
            if (key := systemd_key(config.workloads[logical])) is not None
        }
        revision = self.backend.revision if seeds else None
        try:
            graph = await self.backend.dependencies(seeds) if seeds else Graph()
        except Exception as error:
            graph = Graph(incomplete=[str(error)])
        report = inspect(config, stage, action, graph)
        if epoch != self.epoch or revision is not None and revision != self.backend.revision:
            report["stale"] = True
            report["incomplete"].append("Inputs changed during inspection; retry")
        else:
            self.reports[stage] = (copy.deepcopy(report), epoch, revision)
        return report

    def snapshot(self):
        reports = {}
        for key, (report, epoch, revision) in self.reports.items():
            report = copy.deepcopy(report)
            report["stale"] |= (
                epoch != self.epoch or revision is not None and revision != self.backend.revision
            )
            reports[key] = report
        return copy.deepcopy(
            dict(
                core=self.core,
                config=self.config.json(),
                config_revision=self.config_revision,
                run=self.run,
                observations=self.observations,
                inspections=reports,
                events=list(self.events),
                monitoring=self.backend.health(),
                monitor_gap=self.monitor_gap,
            )
        )

    def update_observer(self, key, old, now):
        """Internal observation gaps do not reset the Lifecycle baseline."""
        if not old:
            return False
        previous = old.get("observer_error") or old.get("children_error")
        error = now.get("observer_error") or now.get("children_error")
        if error and not previous:
            self.emit("warning", f"Internal observation unavailable: {error}", key, "observer_lost")
        elif previous and not error:
            self.emit(
                "info",
                "Internal observation restored; new baseline; intermediate transitions unknown",
                key,
                "observer_restored",
            )
        elif not error:
            before, after = old.get("internal") or {}, now.get("internal") or {}
            if old.get("health") != now.get("health") or any(
                before.get(field) != after.get(field)
                for field in ("state", "canonical_unit", "invocation_id")
            ):
                self.emit(
                    "warning"
                    if now.get("health") == "unhealthy" or after.get("state") == "not_found"
                    else "info",
                    f"Internal observation changed: state={after.get('state', now['state'])}, health={now.get('health')}; no repair requested",
                    key,
                    "observer_changed",
                )
        return not previous and not error

    def update(self, current):
        baseline = not self.known and not self.observations
        for key, now in current.items():
            if now["state"] == "unknown":
                if key not in self.outages and not baseline:
                    self.emit(
                        "warning",
                        "Observation unavailable; history may have a gap",
                        key,
                        "observation_lost",
                    )
                self.outages.add(key)
                continue
            old = self.known.get(key)
            if old and (
                (old["state"] == "not_found") != (now["state"] == "not_found")
                or old["canonical_unit"] != now["canonical_unit"]
            ):
                self.invalidate()
            restored = key in self.outages
            generation = bool(
                old
                and old["manager_generation"] is not None
                and now["manager_generation"] is not None
                and old["manager_generation"] != now["manager_generation"]
            )
            if (restored or generation) and not baseline:
                self.emit(
                    "info",
                    "Observation restored; current state is a new baseline; intermediate transitions unknown",
                    key,
                    "observation_restored",
                )
            elif old and (
                old["state"] != now["state"]
                or old["sub_state"] != now["sub_state"]
                or old["invocation_id"]
                and now["invocation_id"]
                and old["invocation_id"] != now["invocation_id"]
            ):
                recent = self.backend.recent(self.config.workloads[key])
                transition = f"{old['state']} → {now['state']}"
                if (
                    old["invocation_id"]
                    and now["invocation_id"]
                    and old["invocation_id"] != now["invocation_id"]
                ):
                    transition += "; invocation changed"
                self.emit(
                    "warning"
                    if now["state"] == "not_found" or self.busy and not recent
                    else "info",
                    f"{transition} (recent request; causality unconfirmed)"
                    if recent
                    else f"{transition} (not correlated with devdash; origin unknown)",
                    key,
                    "state_changed",
                )
            observer_continuous = not (restored or generation) and self.update_observer(
                key, old, now
            )
            if old and not restored and not generation and observer_continuous:
                prior = {child["id"]: child for child in old["children"]}
                for child in now["children"]:
                    previous = prior.get(child["id"])
                    if (
                        child["kind"] != "process"
                        and previous
                        and previous["epoch"] == child["epoch"]
                        and (previous["state"], previous["pid"]) != (child["state"], child["pid"])
                    ):
                        self.emit(
                            "info",
                            f"Child {child['id']}: {previous['state']} → {child['state']}; origin not proven",
                            key,
                            "child_changed",
                        )
            self.outages.discard(key)
            retained = copy.deepcopy(now)
            if old and not generation:
                for field in ("canonical_unit", "invocation_id"):
                    if retained[field] is None:
                        retained[field] = old[field]
            self.known[key] = retained
        self.observations = current

    async def poll(self):
        config = self.config

        async def read(key, work):
            try:
                value = await asyncio.wait_for(self.backend.observe(work), 5)
            except Exception as error:
                value = observation(error=str(error))
            return key, value

        current = dict(
            await asyncio.gather(*(read(key, work) for key, work in config.workloads.items()))
        )
        if self.config is config:
            self.update(current)

    async def monitor(self):
        async def states():
            while True:
                await self.poll()
                await asyncio.sleep(1)

        async def diagnostics():
            while True:
                config = self.config
                for stage in config.stages:
                    if self.config is not config:
                        break
                    stage_id = stage["id"]
                    old = self.snapshot()["inspections"].get(stage_id)
                    if old is None or old["stale"]:
                        await self.inspect(stage_id, "restart")
                await asyncio.sleep(1)

        await asyncio.gather(states(), diagnostics())
