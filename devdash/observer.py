"""Observation and diagnostics only. No Operator or Plan dependencies."""

import asyncio
import copy
import time
from collections import deque

from .inspection import Graph, inspect
from .model import docker_reference, observation, readiness, systemd_key


class Observer:
    def __init__(self, config, backend):
        self.config, self.backend = config, backend
        self.events = deque(maxlen=200)
        self.sequence = 0
        self.observations, self.known = {}, {}
        self.internal_known = {}
        self.outages = set()
        self.monitor_gap = None
        self.reports = {}
        self.epoch = 0
        self.observed_at = 0.0
        self._poll_lock = asyncio.Lock()
        backend.emit = self.on_backend_event

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
        return event

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
        event.pop("observed_at", None)

        level = "warning" if kind in ("disconnected", "gap") else "info"
        if kind in ("disconnected", "reconnected", "gap"):
            self.monitor_gap = f"{scope or 'Backend'} event history has a gap; current observations form a new baseline"
            for key, value in self.observations.items():
                work = self.config.workloads[key]
                if work["backend"] == "systemd" and (scope is None or work["scope"] == scope):
                    # A signal can precede the next poll. Reconnection alone is
                    # not evidence that the previous Lifecycle state is current.
                    value["lifecycle_stale"] = True
        return self.emit(level, message, workload, kind, **event)

    def invalidate(self):
        self.epoch += 1

    async def inspect(self, stage, action, config=None):
        config = config or self.config
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

    def update_internal(self, key, now):
        work = self.config.workloads[key]
        if not (
            docker_reference(work)
            or work.get("controller")
            or work.get("observer", {}).get("http")
            or now.get("children")
        ):
            return
        value = copy.deepcopy(now)
        if work["backend"] == "docker" and now["state"] == "unknown":
            value["observer_error"] = now.get("error") or "Docker observation unavailable"
        if (
            work.get("observer", {}).get("http")
            and not docker_reference(work)
            and not work.get("controller")
        ):
            if (now.get("http") or {}).get("state", "unknown") == "unknown":
                value["observer_error"] = (now.get("http") or {}).get(
                    "error"
                ) or "HTTP observation unavailable"
        old = self.internal_known.get(key)
        continuous = self.update_observer(key, old, value)
        if old and continuous:
            prior = {child["id"]: child for child in old["children"]}
            for child in value["children"]:
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
        self.internal_known[key] = value

    def update(self, current):
        baseline = not self.known and not self.observations
        for key, now in current.items():
            self.update_internal(key, now)
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
                    "warning" if now["state"] == "not_found" else "info",
                    f"{transition} (recent request; causality unconfirmed)"
                    if recent
                    else f"{transition} (not correlated with devdash; origin unknown)",
                    key,
                    "state_changed",
                )
            self.outages.discard(key)
            retained = copy.deepcopy(now)
            if old and not generation:
                for field in ("canonical_unit", "invocation_id"):
                    if retained[field] is None:
                        retained[field] = old[field]
            self.known[key] = retained
        self.observations = current
        self.observed_at = time.monotonic()

    async def _poll(self):
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

    def inspection_snapshot(self):
        reports = {}
        for key, (report, epoch, revision) in self.reports.items():
            report = copy.deepcopy(report)
            report["stale"] |= (
                epoch != self.epoch or revision is not None and revision != self.backend.revision
            )
            reports[key] = report
        return reports

    async def poll(self):
        async with self._poll_lock:
            await self._poll()

    async def watch(self):
        while True:
            await self.poll()
            yield self.observations
            await asyncio.sleep(1)

    async def diagnostics(self):
        while True:
            config = self.config
            for stage in config.stages:
                if self.config is not config:
                    break
                previous = self.inspection_snapshot().get(stage["id"])
                if previous is None or previous["stale"]:
                    await self.inspect(stage["id"], "restart")
            await asyncio.sleep(1)

    def current(self):
        values = copy.deepcopy(self.observations)
        stale = not self.observed_at or time.monotonic() - self.observed_at > 10
        for value in values.values():
            value["stale"] = stale
        for key, value in values.items():
            if value.get("lifecycle_stale"):
                value.update(
                    state="unknown",
                    sub_state="",
                    pid=None,
                    error="Manager observation gap; awaiting a fresh Lifecycle observation",
                )
                if not docker_reference(self.config.workloads[key]):
                    value["readiness"] = readiness()
        return values
