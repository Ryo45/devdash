"""Plain display text for observed facts and sequence progress; no Tk dependency."""

from .model import Config, docker_reference


def roles(work):
    lifecycle = (
        f"systemd ({work['scope']}): {work['unit']}"
        if work["backend"] == "systemd"
        else "Docker Engine"
    )
    docker = docker_reference(work)
    observer = "Docker API" if docker else "systemd"
    if docker:
        selector = (
            docker.get("container")
            or f"{docker['compose']['project']}/{docker['compose']['service']}"
        )
        observer += f": {selector}"
    controller = work.get("controller")
    child = "None (observation only)"
    if controller:
        location = "container" if docker else "host"
        child = f"{controller['kind']} ({location})"
        observer += f" + {child}"
    return lifecycle, observer, child


def workload_summary(work, observed, connected=True):
    lifecycle, observer, child = roles(work)
    ready = observed.get("readiness", {})
    lines = [
        f"Execution: {observed.get('state', 'unknown')}",
        f"Readiness: {ready.get('state', 'unknown')} · {ready.get('source', 'unavailable')}",
        f"Health: {observed.get('health') or 'not reported'}",
        f"Lifecycle Controller: {lifecycle}",
        f"Internal Observer: {observer}",
        f"Child Controller: {child}",
    ]
    if internal := observed.get("internal"):
        lines.insert(1, f"Container execution: {internal['state']}")
        if internal.get("state") == "unknown":
            lines[3] = "Health: unknown (Docker observation unavailable)"
    if ready.get("detail"):
        lines.append(ready["detail"])
    for field in ("error", "observer_error", "children_error"):
        if error := observed.get(field):
            if error not in lines:
                lines.append(error)
    if not connected:
        lines.insert(0, "STALE: Core disconnected; current execution, health and readiness unknown")
    return "\n".join(lines)


def group_progress(snapshot, group):
    config = Config(snapshot["config"])
    selected = config.expand(group)
    run = snapshot["run"]
    labels = {(step.kind, step.label) for step in selected}
    relevant = [row for row in run.get("plan", []) if (row["kind"], row["step"]) in labels]
    # Child/direct operations are never presented as a completed Group sequence.
    covered = {(row["kind"], row["step"]) for row in relevant}
    if not run.get("group") or not labels <= covered:
        relevant = []
    plan = relevant or [dict(step=s.label, kind=s.kind, action="start") for s in selected]
    progress = (
        {(p["action"], p.get("kind"), p["step"]): p for p in run["progress"]} if relevant else {}
    )
    next_marked = False
    lines = ["Group sequence · ordered Steps (Parallel members run together)"]
    for number, row in enumerate(plan, 1):
        entry = progress.get((row["action"], row["kind"], row["step"]), {})
        status = entry.get(
            "status",
            "pending"
            if relevant and run["status"] in ("running", "cancelling")
            else "not executed",
        )
        label = row["step"]
        if row["kind"] == "gate":
            manual = config.gates[label]["type"] == "manual"
            label = ("Approval: " if manual else "Readiness check: ") + label
            if row["action"] == "stop":
                label += " (skipped on Stop)"
            elif status == "running" and run["waiting_gate"] == row["step"]:
                status = "WAITING FOR CONTINUE" if manual else "WAITING FOR READINESS"
        if status == "pending" and not next_marked and run["status"] == "running":
            status = "NEXT"
            next_marked = True
        lines.append(f"{number:02d}  [{status}] {row['action']} · {row['kind']} · {label}")
        if entry.get("error"):
            lines.append("    " + entry["error"])
    if not relevant:
        if run["status"]:
            lines.append(
                f"Current/last operation: {run['target']} {run.get('action', '')} · {run['status']}"
            )
        lines.append(
            "No Group sequence result for this selection. Service state is shown separately."
        )
    elif run.get("error"):
        lines.append("Sequence error: " + run["error"])
    return "\n".join(lines)
