"""Plain display text for observed facts and sequence progress; no Tk dependency."""

import json

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
    if observed.get("http"):
        lines.append("HTTP observation: " + str(observed["http"]))
    if ready.get("detail"):
        lines.append(ready["detail"])
    for field in ("error", "observer_error", "children_error"):
        if error := observed.get(field):
            if error not in lines:
                lines.append(error)
    if not connected or observed.get("stale"):
        lines.insert(
            0, "STALE: current execution, health and readiness unknown; values below are historical"
        )
    return "\n".join(lines)


def stage_progress(snapshot, key):
    config = Config(snapshot["config"])
    stage = config.stage(key)
    run = snapshot["run"]
    relevant = run["kind"] == "stage" and run["target"] == key
    progress = {p["workload"]: p for p in run["progress"]} if relevant else {}
    lines = [
        f"Stage: {stage['name']}",
        stage["description"],
        "Results refer only to the current/last request; previous Stage successes are not latched.",
    ]
    lines.extend("Check: " + item for item in stage["checks"])
    for member in stage["workloads"]:
        entry = progress.get(member, {})
        lines.append(
            f"[{entry.get('status', 'pending' if relevant and run['status'] == 'running' else 'not requested')}] {member}"
            + (": " + entry["error"] if entry.get("error") else "")
        )
    if relevant:
        lines.append(
            "Operation: " + run["status"] + (": " + run["error"] if run.get("error") else "")
        )
    try:
        following = config.next_stage(key)
        lines.append(
            f"Next Stage (explicit request only): {following['name']} · "
            + ", ".join(following["workloads"])
        )
    except ValueError:
        lines.append("Last Stage; no Next request available")
    return "\n".join(lines)


def plan_progress(snapshot):
    plan = snapshot.get("plan")
    if plan is None:
        summary = snapshot["run"].get("plan_result")
        if not summary:
            return "No active Plan. Normal Workload/Stage controls are independent."
        return (
            "Plan ended: "
            + summary["status"]
            + "; execution state and confirmation discarded.\n"
            + json.dumps(summary, indent=2, ensure_ascii=False)
            + "\nIssued operations and adapter results:\n"
            + json.dumps(snapshot["run"]["progress"], indent=2, ensure_ascii=False)
        )
    lines = [
        f"Run: {plan['id']} · Template: {plan['template']}",
        f"Config revision: {plan['config_revision']} · {plan['config_hash']}",
        "Remaining steps are pending; no rollback on failure or abort.",
    ]
    for index, step in enumerate(plan["steps"], 1):
        lines.append(
            f"{index:02d} [{step['status']}] {step.get('action', 'Checkpoint')} {step['stage']} · {', '.join(step['workloads'])}"
        )
        lines.extend("  Check: " + item for item in step.get("checks", []))
        for field, label in (("preview", "Preview"), ("assessment", "Actual decision")):
            for key, decision in step.get(field, {}).items():
                lines.append(f"  {label}: {key} {decision['action']} — {decision['reason']}")
        if step.get("error"):
            lines.append("  " + step["error"])
    lines.extend(
        [
            "Issued operations and adapter results:",
            json.dumps(snapshot["run"]["progress"], indent=2, ensure_ascii=False),
        ]
    )
    if checkpoint := plan.get("checkpoint"):
        lines.extend(
            [
                "WAITING FOR RUN-LOCAL CONTINUE",
                checkpoint["notice"],
                "UNKNOWN / STALE observations"
                if checkpoint["unknown"]
                else "Fresh observations; human confirmation is not a safety interlock",
                json.dumps(checkpoint["observations"], indent=2, ensure_ascii=False),
            ]
        )
    return "\n".join(lines)
