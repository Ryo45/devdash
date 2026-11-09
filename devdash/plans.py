"""Finite standard Plans, generated on demand. No DSL or durable Run state."""

import asyncio
import copy
import hashlib
import json
import uuid
from collections import OrderedDict

from .model import Config

KINDS = {"startup", "restart_from", "stop_from"}


def steps_for(config, kind, from_stage=None):
    if kind not in KINDS:
        raise ValueError("Unknown standard Plan kind")
    if kind == "startup":
        if from_stage is not None:
            raise ValueError("Startup uses all Stages; no from_stage")
        stages = config.stages
    else:
        first = config.stage(from_stage)
        stages = config.stages[config.stages.index(first) :]
    steps = []
    if kind in ("restart_from", "stop_from"):
        steps.extend(
            dict(kind="operation", stage=s["id"], action="stop", workloads=list(s["workloads"]))
            for s in reversed(stages)
        )
    if kind != "stop_from":
        for index, stage in enumerate(stages):
            steps.append(
                dict(
                    kind="operation",
                    stage=stage["id"],
                    action="start",
                    workloads=list(stage["workloads"]),
                )
            )
            if index < len(stages) - 1 and stage["checks"]:
                boundary = config.stages.index(stage)
                watched = [key for s in config.stages[: boundary + 1] for key in s["workloads"]]
                steps.append(
                    dict(
                        kind="checkpoint",
                        stage=stage["id"],
                        checks=list(stage["checks"]),
                        workloads=watched,
                    )
                )
    return steps


class PlanExecutor:
    def __init__(self, operator, observer, revision):
        self.operator, self.observer, self.revision = operator, observer, revision
        self.previews = OrderedDict()
        self.active = None

    def preview(self, kind=None, from_stage=None, template=None):
        config = Config(self.operator.config.json())
        if template is not None:
            if kind is not None or from_stage is not None:
                raise ValueError("Choose a named Template or a standard kind")
            if template not in config.plan_templates:
                raise ValueError("Unknown Plan Template")
            definition = config.plan_templates[template]
            kind, from_stage = definition["kind"], definition.get("from_stage")
        kind = kind or "startup"
        plan = dict(
            id=uuid.uuid4().hex,
            template=template or kind,
            kind=kind,
            from_stage=from_stage,
            config_revision=self.revision(),
            config_hash=hashlib.sha256(
                json.dumps(config.json(), sort_keys=True).encode()
            ).hexdigest(),
            steps=steps_for(config, kind, from_stage),
            note="Preview only. Empty checks add no Checkpoint; final Stage has no Checkpoint. systemd may affect units outside the requested scope.",
        )
        self.previews[plan["id"]] = (copy.deepcopy(plan), config)
        while len(self.previews) > 8:
            self.previews.popitem(last=False)
        return copy.deepcopy(plan)

    def start(self, preview_id):
        if preview_id not in self.previews:
            raise ValueError("Unknown/consumed Plan preview; create and review a new preview")
        preview, config = self.previews[preview_id]
        if preview["config_revision"] != self.revision():
            raise ValueError("Plan configuration changed; preview again")
        run = dict(
            id=uuid.uuid4().hex,
            template=preview["template"],
            kind=preview["kind"],
            config_revision=preview["config_revision"],
            config_hash=preview["config_hash"],
            steps=[dict(step, status="pending", error=None) for step in preview["steps"]],
            checkpoint=None,
            config=config,
            signal=asyncio.Event(),
            confirmed=None,
            leases={},
        )
        self.operator._launch(run["id"], "plan", preview["kind"], lambda: self.execute(run))
        self.previews.pop(preview_id)
        self.active = run
        return run["id"]

    def require_run(self, run_id):
        if self.active is None or self.active["id"] != run_id:
            raise ValueError("Plan Run changed or ended; refresh before operating")
        return self.active

    def cancel(self, run_id):
        self.require_run(run_id)
        self.operator.request_cancel()
        self.wake()

    def wake(self):
        if self.active:
            self.active["signal"].set()

    def facts(self, members):
        values = self.observer.current()
        result = {}
        for key in members:
            value = values.get(key, {})
            internal = value.get("internal") or {}
            result[key] = dict(
                state=value.get("state", "unknown"),
                health=value.get("health"),
                readiness=value.get("readiness"),
                stale=value.get("stale", True),
                manager_generation=value.get("manager_generation"),
                canonical_unit=value.get("canonical_unit"),
                invocation_id=value.get("invocation_id"),
                internal_state=internal.get("state"),
                internal_id=internal.get("canonical_unit"),
                internal_invocation=internal.get("invocation_id"),
                internal_unavailable=bool(
                    value.get("observer_error") or value.get("children_error")
                ),
                http_state=(value.get("http") or {}).get("state"),
                children=[
                    dict(id=c["id"], epoch=c["epoch"], state=c["state"], pid=c.get("pid"))
                    for c in value.get("children", [])
                    if c["kind"] != "process"
                ],
            )
        return result

    def observations_changed(self, notice=None):
        run = self.active
        if run is None or run["checkpoint"] is None:
            return
        checkpoint = run["checkpoint"]
        facts = self.facts(checkpoint["workloads"])
        fingerprint = json.dumps(facts, sort_keys=True)
        if fingerprint != checkpoint["fingerprint"] or notice:
            checkpoint.update(
                token=uuid.uuid4().hex,
                fingerprint=fingerprint,
                observations=facts,
                notice=notice or "Observed state or generation changed; review again",
            )
            run["confirmed"] = None
        checkpoint["unknown"] = any(
            v["state"] == "unknown"
            or v["stale"]
            or v["internal_unavailable"]
            or (v.get("readiness") or {}).get("state", "unknown") == "unknown"
            for v in facts.values()
        )
        if any(lease.is_set() for lease in run["leases"].values()):
            checkpoint["notice"] = (
                "Manager changed; operation outcome unknown; Plan will not resume"
            )
            run["signal"].set()

    def on_backend_event(self, event):
        run = self.active
        if run is None or run["checkpoint"] is None:
            return
        if event.get("workload") in run["checkpoint"]["workloads"] and event["kind"] in (
            "job",
            "invocation_changed",
            "unit_removed",
        ):
            self.observations_changed(
                "Backend reported a change during Checkpoint; refresh and review again"
            )
        elif event["kind"] in ("disconnected", "reconnected", "gap", "reloading", "reloaded"):
            scopes = {
                run["config"].workloads[key]["scope"]
                for key in run["checkpoint"]["workloads"]
                if run["config"].workloads[key]["backend"] == "systemd"
            }
            if event.get("scope") is not None and event["scope"] not in scopes:
                return
            self.observations_changed("Manager connection/configuration changed during Checkpoint")
            self.wake()

    async def continue_run(self, run_id, token):
        run = self.require_run(run_id)
        if (
            run["checkpoint"] is None
            or self.operator.cancel.is_set()
            or run["confirmed"] is not None
        ):
            raise ValueError("No Checkpoint waiting")
        await self.observer.poll()  # Fresh evidence for confirmation; never an operation result.
        self.require_run(run_id)
        self.observations_changed()
        if self.operator.cancel.is_set():
            raise ValueError("Plan is cancelling; Checkpoint will not continue")
        checkpoint = run["checkpoint"]
        if checkpoint is None or checkpoint["token"] != token:
            raise ValueError("Checkpoint observations changed; refresh and confirm again")
        if any(lease.is_set() for lease in run["leases"].values()):
            self.wake()
            raise RuntimeError("Manager changed; Plan outcome unknown")
        if run["confirmed"] is not None:
            raise ValueError("Checkpoint already acknowledged")
        run["confirmed"] = token
        run["signal"].set()

    async def checkpoint(self, run, step):
        await self.observer.poll()
        run["checkpoint"] = dict(
            token=uuid.uuid4().hex,
            stage=step["stage"],
            checks=step["checks"],
            workloads=step["workloads"],
            fingerprint="",
            observations={},
            notice="",
            unknown=True,
        )
        self.observations_changed()
        # This wait exists only for this Run; cancellation and lost manager release it.
        lost = [asyncio.create_task(lease.wait()) for lease in run["leases"].values()]
        try:
            while not self.operator.cancel.is_set():
                if any(lease.is_set() for lease in run["leases"].values()):
                    raise RuntimeError(
                        "Manager lost during Checkpoint; outcome unknown; Plan will not resume"
                    )
                if run["confirmed"] == run["checkpoint"]["token"]:
                    return
                run["signal"].clear()
                signal = asyncio.create_task(run["signal"].wait())
                cancel = asyncio.create_task(self.operator.cancel.wait())
                try:
                    await asyncio.wait([signal, cancel, *lost], return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in (signal, cancel):
                        task.cancel()
                    await asyncio.gather(signal, cancel, return_exceptions=True)
        finally:
            for task in lost:
                task.cancel()
            await asyncio.gather(*lost, return_exceptions=True)
            run["checkpoint"] = run["confirmed"] = None

    async def execute(self, run):
        outcome = "interrupted"
        try:
            scopes = {
                work["scope"]
                for step in run["steps"]
                if step["kind"] == "operation"
                for key in step["workloads"]
                if (work := run["config"].workloads[key])["backend"] == "systemd"
            }
            run["leases"] = {scope: self.operator.backend.lease(scope) for scope in scopes}
            for step in run["steps"]:
                if self.operator.cancel.is_set():
                    step["status"] = "skipped"
                    continue
                step["status"] = "running"
                try:
                    if step["kind"] == "operation":
                        first = len(self.operator.run["progress"])
                        await self.operator.batch(
                            step["stage"],
                            step["workloads"],
                            True,
                            step["action"],
                            leases=run["leases"],
                            config=run["config"],
                        )
                        rows = self.operator.run["progress"][first:]
                        skipped = any(row["status"] == "skipped" for row in rows)
                        step["status"] = (
                            "partial"
                            if skipped and any(row["status"] == "completed" for row in rows)
                            else "skipped"
                            if skipped
                            else "completed"
                        )
                    else:
                        await self.checkpoint(run, step)
                        step["status"] = "skipped" if self.operator.cancel.is_set() else "completed"
                except Exception as error:
                    step.update(status="failed", error=str(error))
                    raise
            outcome = "cancelled" if self.operator.cancel.is_set() else "completed"
        except Exception:
            outcome = "failed"
            raise
        finally:
            for step in run["steps"]:
                if step["status"] == "pending":
                    step["status"] = "skipped"
                elif step["status"] == "running":
                    step["status"] = "unknown"
            self.operator.run["plan_result"] = dict(
                id=run["id"],
                template=run["template"],
                status=outcome,
                steps=copy.deepcopy(run["steps"]),
            )
            # No execution state, configuration snapshot, lease or confirmation survives.
            run["checkpoint"] = run["confirmed"] = None
            run["leases"].clear()
            self.active = None

    def snapshot(self):
        if self.active is None:
            return None
        self.observations_changed()
        run = self.active
        return copy.deepcopy(
            {
                key: value
                for key, value in run.items()
                if key not in ("config", "signal", "leases", "confirmed")
            }
        )
