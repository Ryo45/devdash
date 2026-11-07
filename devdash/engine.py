"""Async, GUI-independent ordered Steps. Never supervises a process."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from collections import deque
from pathlib import Path

import aiohttp

from .inspection import Graph, inspect
from .model import Config, Step, observation, step_workloads, systemd_key

ACTIONS = {"start", "stop", "restart", "reinitialize"}


class Engine:
    def __init__(self, config: Config, backend, state_file: Path | None = None, boot: str = "test"):
        self.config, self.backend = config, backend
        self.state_file, self.boot = state_file, boot
        self.approved: set[str] = set()
        if state_file and state_file.exists():
            value = json.loads(state_file.read_text())
            if value.get("boot") == boot:
                if not isinstance(value.get("approved"), list) or not all(
                    isinstance(g, str) for g in value["approved"]
                ):
                    raise ValueError("Invalid persisted Gate approvals")
                self.approved = set(value["approved"])
        self.cancel = asyncio.Event()
        self.changed = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.run = dict(target="", status="", progress=[], waiting_gate=None, error=None)
        self.events = deque(maxlen=200)
        self.sequence = 0
        self.observations: dict[str, dict] = {}
        self.known: dict[str, dict] = {}
        self.outages: set[str] = set()
        self.gate_issues: set[tuple[str, str]] = set()
        self.monitor_gap = None
        self.reports: dict[str, tuple[dict, int, int | None]] = {}
        self.epoch = 0
        self.started = self.finished = 0.0
        backend.emit = self.on_backend_event

    @property
    def busy(self) -> bool:
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

    def persist(self, approvals):
        if self.state_file:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_file.with_suffix(".tmp")
            temporary.write_text(json.dumps({"boot": self.boot, "approved": sorted(approvals)}))
            temporary.replace(self.state_file)

    def approve(self, gate):
        if self.config.gates.get(gate, {}).get("type") != "manual":
            raise ValueError(f"Not a Manual Gate: {gate}")
        approvals = self.approved | {gate}
        self.persist(approvals)
        self.approved = approvals
        self.invalidate()
        self.changed.set()

    def request_cancel(self):
        if not self.busy:
            raise ValueError("No operation is running")
        self.cancel.set()
        self.run["status"] = "cancelling"
        self.changed.set()

    def launch(self, target: str, group: bool, action: str):
        if action not in ACTIONS or (action == "reinitialize" and not group):
            raise ValueError("Invalid operation; Reinitialize requires a Group")
        if target not in (self.config.groups if group else self.config.workloads):
            raise ValueError("Unknown operation target")
        self._launch(target, action, lambda: self.execute(target, group, action))

    def launch_child(self, parent, child, epoch, action):
        if parent not in self.config.workloads or action not in ACTIONS - {"reinitialize"}:
            raise ValueError("Invalid child operation")

        async def operation():
            if not self.cancel.is_set():
                await self.track(
                    child,
                    action,
                    self.backend.operate_child(self.config.workloads[parent], child, epoch, action),
                )

        self._launch(f"{parent}/{child}", action, operation)

    def _launch(self, target, action, operation):
        if self.busy:
            raise ValueError("Another operation is running")
        self.cancel = asyncio.Event()
        self.started, self.finished = time.monotonic(), 0.0
        self.run = dict(target=target, status="running", progress=[], waiting_gate=None, error=None)

        async def run():
            try:
                await operation()
                self.run["status"] = "cancelled" if self.cancel.is_set() else "completed"
            except asyncio.CancelledError:
                raise  # Core shutdown never sends systemd CancelJob or rollback.
            except Exception as error:
                self.run.update(status="failed", error=str(error))
            finally:
                self.run["waiting_gate"] = None
                self.finished = time.monotonic()

        self.task = asyncio.create_task(run(), name=f"operation:{target}:{action}")

    async def wait(self):
        if self.task:
            await self.task
        return copy.deepcopy(self.run)

    async def track(self, label, action, operation, gate=False):
        progress = dict(step=label, action=action, status="running", error=None)
        self.run["progress"].append(progress)
        try:
            result = await operation
        except Exception as error:
            progress.update(status="failed", error=str(error))
            raise
        else:
            progress["status"] = "skipped" if gate and self.cancel.is_set() else "completed"
            return result

    async def execute(self, target, group, action):
        phases = ["stop", "start"] if group and action in ("restart", "reinitialize") else [action]
        plan = [
            (phase, step)
            for phase in phases
            for step in (
                self.config.expand(target, phase == "stop") if group else [Step("workload", target)]
            )
        ]
        scopes = {
            self.config.workloads[key]["scope"]
            for _, step in plan
            for key in step.workloads
            if self.config.workloads[key]["backend"] == "systemd"
        }
        leases = {scope: self.backend.lease(scope) for scope in scopes}
        if group and not self.cancel.is_set():
            report = await self.inspect(target, action)
            if not self.cancel.is_set() and (
                report["stale"] or any(d["level"] == "error" for d in report["diagnostics"])
            ):
                raise RuntimeError(
                    "Dependency inspection blocked operation: "
                    + "; ".join(
                        d["message"] for d in report["diagnostics"] if d["level"] == "error"
                    )
                    + (" (stale)" if report["stale"] else "")
                )
        if action == "reinitialize" and not self.cancel.is_set():
            approvals = self.approved - {
                str(s.value) for s in self.config.expand(target) if s.kind == "gate"
            }
            self.persist(approvals)
            self.approved = approvals
            self.invalidate()
        failure = None
        for phase, step in plan:
            if failure is None and any(lease.is_set() for lease in leases.values()):
                failure = RuntimeError(
                    "Manager connection lost; outcome unknown; sequence will not resume"
                )
            if failure or self.cancel.is_set() or phase == "stop" and step.kind == "gate":
                self.run["progress"].append(
                    dict(step=step.label, action=phase, status="skipped", error=None)
                )
                continue
            try:
                await self.track(
                    step.label, phase, self.run_step(step, phase, leases), gate=step.kind == "gate"
                )
            except Exception as error:
                failure = error
        if failure:
            raise failure

    async def run_step(self, step, action, leases):
        async def workload(key):
            work = self.config.workloads[key]
            lease = leases.get(work["scope"]) if work["backend"] == "systemd" else None
            await self.backend.operate(work, action, lease)

        if step.kind == "workload":
            return await workload(str(step.value))
        if step.kind == "parallel":
            results = await asyncio.gather(
                *(self.track(key, action, workload(key)) for key in step.value),
                return_exceptions=True,
            )
            errors = [str(result) for result in results if isinstance(result, BaseException)]
            if errors:
                raise RuntimeError("; ".join(errors))
        elif step.kind == "gate":
            gate = asyncio.create_task(self.gate(str(step.value)))
            lost = [asyncio.create_task(lease.wait()) for lease in leases.values()]
            try:
                done, _ = await asyncio.wait([gate, *lost], return_when=asyncio.FIRST_COMPLETED)
                if any(task in done for task in lost):
                    raise RuntimeError(
                        "Manager lost during Gate wait; outcome unknown; sequence will not resume"
                    )
                await gate
            finally:
                for task in [gate, *lost]:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(gate, *lost, return_exceptions=True)
                self.run["waiting_gate"] = None

    async def gate(self, key):
        gate = self.config.gates[key]
        self.run["waiting_gate"] = key
        if gate["type"] == "manual":
            while key not in self.approved and not self.cancel.is_set():
                self.changed.clear()
                if key in self.approved or self.cancel.is_set():
                    break
                await self.changed.wait()
            return

        async def poll():
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
                while not self.cancel.is_set():
                    try:
                        if gate["type"] == "http":
                            async with session.get(gate["url"], allow_redirects=False) as response:
                                if response.status == gate["expected_status"]:
                                    return
                        elif await self.backend.readiness(self.config.workloads[gate["workload"]]):
                            return
                    except (OSError, TimeoutError, asyncio.TimeoutError, aiohttp.ClientError):
                        pass
                    try:
                        await asyncio.wait_for(self.cancel.wait(), gate["interval_ms"] / 1000)
                    except asyncio.TimeoutError:
                        pass

        check = asyncio.create_task(asyncio.wait_for(poll(), gate["timeout_ms"] / 1000))
        cancel = asyncio.create_task(self.cancel.wait())
        try:
            done, _ = await asyncio.wait([check, cancel], return_when=asyncio.FIRST_COMPLETED)
            if check in done:
                try:
                    await check
                except asyncio.TimeoutError:
                    raise RuntimeError(f"Readiness Gate {key} timed out") from None
        finally:
            for task in (check, cancel):
                if not task.done():
                    task.cancel()
            await asyncio.gather(check, cancel, return_exceptions=True)

    async def inspect(self, group, action):
        epoch, approvals = self.epoch, set(self.approved)
        seeds = {
            key
            for logical in step_workloads(self.config.expand(group))
            if (key := systemd_key(self.config.workloads[logical])) is not None
        }
        revision = self.backend.revision if seeds else None
        try:
            graph = await self.backend.dependencies(seeds) if seeds else Graph()
        except Exception as error:
            graph = Graph(incomplete=[str(error)])
        report = inspect(self.config, group, action, approvals, graph)
        if epoch != self.epoch or revision is not None and revision != self.backend.revision:
            report["stale"] = True
            report["incomplete"].append("Inputs changed during inspection; retry")
        else:
            self.reports[group] = (copy.deepcopy(report), epoch, revision)
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
                config=self.config.json(),
                run=self.run,
                approved=sorted(self.approved),
                observations=self.observations,
                inspections=reports,
                events=list(self.events),
                monitoring=self.backend.health(),
                monitor_gap=self.monitor_gap,
            )
        )

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
                self.emit(
                    "warning"
                    if now["state"] == "not_found" or self.busy and not recent
                    else "info",
                    f"{old['state']} → {now['state']} (recent request; causality unconfirmed)"
                    if recent
                    else f"{old['state']} → {now['state']} (not correlated with devdash; origin unknown)",
                    key,
                    "state_changed",
                )
            if old and not restored and not generation:
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
        issues = {
            (gate, key)
            for gate, key in self.config.manual_followers()
            if gate not in self.approved
            and current.get(key, {}).get("state") in ("active", "activating", "reloading")
        }
        issues |= {
            (gate, key)
            for gate, key in self.gate_issues
            if gate not in self.approved and current.get(key, {}).get("state") == "unknown"
        }
        for gate, key in issues - self.gate_issues:
            self.emit(
                "warning",
                f"Manual Gate {gate} is unapproved, but {key} is running; observed mismatch, origin unknown",
                key,
                "gate_mismatch",
            )
        self.gate_issues = issues
        self.observations = current

    async def poll(self):
        async def read(key, work):
            try:
                value = await asyncio.wait_for(self.backend.observe(work), 5)
            except Exception as error:
                value = observation(error=str(error))
            return key, value

        current = dict(
            await asyncio.gather(*(read(key, work) for key, work in self.config.workloads.items()))
        )
        self.update(current)

    async def monitor(self):
        async def states():
            while True:
                await self.poll()
                await asyncio.sleep(1)

        async def diagnostics():
            while True:
                for group in self.config.groups:
                    old = self.snapshot()["inspections"].get(group)
                    if old is None or old["stale"]:
                        await self.inspect(group, "restart")
                await asyncio.sleep(1)

        await asyncio.gather(states(), diagnostics())
