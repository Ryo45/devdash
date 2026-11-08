"""One-way, offline migration proposals. No legacy runtime Coordinator."""

from .model import Config, fields, parse_yaml, require


def migrate(text):
    raw = fields(parse_yaml(text), {"workloads", "groups", "gates"}, "legacy config")
    workloads, groups, gates = raw.get("workloads", {}), raw.get("groups", {}), raw.get("gates", {})
    require(isinstance(groups, dict) and isinstance(gates, dict), "Invalid legacy groups/gates")
    stages, used_work, used_groups, used_gates = [], set(), set(), set()
    require(isinstance(workloads, dict), "Invalid legacy Workloads")
    for value in gates.values():
        require(
            isinstance(value, dict) and value.get("type") in ("manual", "http", "container_ready"),
            "Unknown legacy Gate type",
        )
    readiness = []
    leading_checks = []

    def expand(key, active):
        require(isinstance(key, str), "Invalid Group reference")
        require(
            key in groups and key not in active and key not in used_groups,
            f"Unknown, duplicate or cyclic Group: {key}",
        )
        used_groups.add(key)
        group = fields(groups[key], {"steps", "name"}, "legacy Group")
        require(isinstance(group.get("steps"), list), "Group needs steps")
        for index, step in enumerate(group["steps"]):
            require(isinstance(step, dict) and len(step) == 1, "Invalid legacy Step")
            kind, value = next(iter(step.items()))
            if kind == "group":
                expand(value, active | {key})
            elif kind in ("workload", "parallel"):
                members = [value] if kind == "workload" else value
                require(
                    isinstance(members, list)
                    and members
                    and all(isinstance(v, str) and v in workloads for v in members),
                    "Unknown legacy Workload",
                )
                require(
                    not used_work.intersection(members) and len(set(members)) == len(members),
                    "Duplicate legacy Workload placement",
                )
                used_work.update(members)
                name = f"stage-{len(stages) + 1}"
                stages.append(
                    dict(
                        id=name,
                        name=group.get("name") or str(key),
                        description=f"Migrated {key}, Step {index + 1}; review Stage boundary",
                        checks=list(leading_checks),
                        workloads=members,
                    )
                )
                leading_checks.clear()
            elif kind == "gate":
                require(isinstance(value, str), "Invalid Gate reference")
                require(value in gates and value not in used_gates, "Unknown/duplicate legacy Gate")
                used_gates.add(value)
                gate = gates[value]
                require(
                    isinstance(gate, dict)
                    and gate.get("type") in ("manual", "http", "container_ready"),
                    "Unknown legacy Gate type",
                )
                if gate["type"] == "manual":
                    if stages:
                        stages[-1]["checks"].append("Before starting the next Stage: " + value)
                    else:
                        leading_checks.append("Before starting this Stage: " + value)
                else:
                    readiness.append(
                        dict(
                            gate=value,
                            condition=gate,
                            action="Delegate mandatory readiness to the owning unit/runtime before adopting this proposal",
                        )
                    )
            else:
                raise ValueError("Unsupported legacy Step: " + kind)

    children = set()
    for group in groups.values():
        require(
            isinstance(group, dict) and isinstance(group.get("steps"), list), "Invalid legacy Group"
        )
        for step in group["steps"]:
            require(isinstance(step, dict) and len(step) == 1, "Invalid legacy Step")
            if "group" in step:
                require(isinstance(step["group"], str), "Invalid Group reference")
                children.add(step["group"])
    for key in groups:
        if key not in children:
            expand(key, set())
    require(used_groups == set(groups), "Cyclic/unreachable legacy Groups")
    for key in gates.keys() - used_gates:
        if gates[key]["type"] != "manual":
            readiness.append(
                dict(
                    gate=key,
                    condition=gates[key],
                    action="Unused readiness definition: review before removing",
                )
            )
    candidate = Config(dict(workloads=workloads, stages=stages, dependencies={}))
    return dict(
        config=candidate.json(),
        warnings=[
            "Proposal only: review every Stage boundary and explicitly declare mandatory dependencies; old order is not evidence of a dependency.",
            "Manual approvals, boot state and interrupted operations are not migrated.",
            "Sequential Workload Steps became separate Stages; Parallel members remain in one Stage.",
            "Unused Workloads remain unassigned; multiple root Groups use their source display order.",
        ],
        readiness_actions=readiness,
    )
