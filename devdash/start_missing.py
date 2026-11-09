"""Pure current-state policy for one-shot startup; no Run history or lifecycle calls."""

from .model import docker_reference


def assess(work, value):
    state = value.get("state", "unknown")
    action, reason = "blocked", f"Execution {state}; observe or resolve before starting"
    ready = value.get("readiness") or {}
    internal = value.get("internal") or {}
    if value.get("stale", True) or value.get("lifecycle_stale"):
        reason = "Stale observation; re-observe before deciding"
    elif value.get("error"):
        reason = "Execution observation unavailable: " + str(value["error"])
    elif state in ("inactive", "failed"):
        action, reason = "start", f"Execution {state}"
    elif state == "active":
        error = value.get("error") or value.get("observer_error") or value.get("children_error")
        if error:
            reason = "Observation unavailable: " + error
        elif value.get("health") not in (None, "healthy"):
            reason = "Health " + str(value["health"])
        elif docker_reference(work) and internal and internal.get("state") != "active":
            reason = "Container execution " + str(internal.get("state", "unknown"))
        elif ready.get("state") == "not_ready":
            reason = "Readiness not_ready: " + str(ready.get("detail", ""))
        elif (
            work.get("observer", {}).get("http")
            and (value.get("http") or {}).get("state") != "ready"
        ):
            reason = "HTTP observation not ready or unknown"
        elif ready.get("state", "unknown") != "ready" and (
            docker_reference(work) or ready.get("source", "unavailable") != "unavailable"
        ):
            reason = "Configured readiness is unknown"
        else:
            action = "skip"
            reason = "Already active"
            if ready.get("state") != "ready":
                reason += "; application readiness not provided; manual checks still apply"
    return dict(
        action=action,
        reason=reason,
        state=state,
        identity={
            field: value.get(field)
            for field in ("manager_generation", "canonical_unit", "invocation_id")
        }
        | {
            "internal_id": internal.get("canonical_unit"),
            "internal_invocation": internal.get("invocation_id"),
        },
    )


def changed(previous, current):
    """Reducing a planned Start to Skip is safe; losing/replacing an active unit is not."""
    return (
        previous["action"] == "blocked"
        or previous["action"] == "skip"
        and (current["action"] != "skip" or previous["identity"] != current["identity"])
    )
