"""Single execution slot and authoritative adapter results, shared by all operations."""

import asyncio
import copy
import time
import uuid

ACTIONS = {"start", "stop", "restart"}


class Operator:
    def __init__(self, config, backend, inspector):
        self.config, self.backend, self.inspector = config, backend, inspector
        self.cancel = asyncio.Event()
        self.task = None
        self.run = dict(target="", kind="", action="", status="", progress=[], error=None)
        self.started = self.finished = 0.0
        self.disturbances = []

    @property
    def busy(self):
        return self.task is not None and not self.task.done()

    def on_backend_event(self, event):
        if not self.busy:
            return
        rows = [
            p
            for p in self.run["progress"]
            if p["workload"] == event.get("workload") and p["status"] == "running"
        ]
        if not rows:
            return
        row = rows[-1]
        if event["kind"] == "job":
            if event.get("correlation") == "requested":
                row["jobs"].append(dict(path=event.get("job_path"), result=event.get("job_result")))
            else:
                self.disturbances.append(event["message"])
        elif event["kind"] == "operation_prepared":
            row["expected_generations"] = (
                0
                if row["action"] == "stop"
                or row["action"] == "start"
                and event.get("active_state") in ("active", "activating")
                else 1
            )
        elif event["kind"] == "invocation_changed":
            value = event["invocation_id"]
            if value not in row["invocations"]:
                row["invocations"].append(value)
            if len(row["invocations"]) > row.get("expected_generations", 1):
                self.disturbances.append(
                    "Multiple process generations during one job: " + row["workload"]
                )

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
            lambda: self.batch(target, members, stage, action),
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
            id=uuid.uuid4().hex,
            target=target,
            kind=kind,
            action=action,
            status="running",
            progress=[],
            error=None,
        )
        self.disturbances = []

        async def run():
            try:
                await operation()
                if self.disturbances:
                    raise RuntimeError("Uncorrelated changes; operation outcome uncertain")
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
        progress = dict(
            id=uuid.uuid4().hex,
            workload=key,
            action=action,
            status="running",
            error=None,
            dispatch="submitted",
            jobs=[],
            invocations=[],
        )
        self.run["progress"].append(progress)
        try:
            result = await operation
        except Exception as error:
            progress.update(status="failed", error=str(error))
            raise
        else:
            progress["status"] = "skipped" if result is False else "completed"
            progress["dispatch"] = "not_issued" if result is False else "finished"

    async def batch(
        self, target, members, stage, action, *, leases=None, config=None, prepare=None
    ):
        config = config or self.config
        scopes = {
            config.workloads[key]["scope"]
            for key in members
            if config.workloads[key]["backend"] == "systemd"
        }
        if leases is None:
            leases = {scope: self.backend.lease(scope) for scope in scopes}
        if any(lease.is_set() for lease in leases.values()):
            raise RuntimeError("Manager changed; operation outcome unknown; not resumed")
        if stage and not self.cancel.is_set():
            report = await self.inspector(target, action, config=config)
            if not self.cancel.is_set() and (
                report["stale"]
                or bool(report["uninspected_units"])
                or any(d["level"] == "error" for d in report["diagnostics"])
            ):
                self.run["progress"].extend(
                    [
                        dict(
                            workload=key,
                            action=action,
                            status="skipped",
                            error="Inspection blocked operation",
                        )
                        for key in members
                    ]
                )
                raise RuntimeError(
                    "Dependency inspection blocked operation: "
                    + "; ".join(
                        [d["message"] for d in report["diagnostics"] if d["level"] == "error"]
                        + report["incomplete"]
                    )
                )
        if prepare is not None and not self.cancel.is_set():
            members = await prepare()
        if self.cancel.is_set():
            self.run["progress"].extend(
                [dict(workload=key, action=action, status="skipped", error=None) for key in members]
            )
            return

        async def workload(key):
            work = config.workloads[key]
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
        if self.disturbances:
            errors.append(
                "Uncorrelated operation or multiple invocations observed; outcome uncertain: "
                + "; ".join(self.disturbances)
            )
        if errors:
            raise RuntimeError("; ".join(errors))
