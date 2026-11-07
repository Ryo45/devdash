"""Pure diagnostics. These edges never determine sequence execution order."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .model import Config, UnitKey, systemd_key

START = {"Wants", "Requires", "BindsTo", "Upholds"}
STRONG = {"Requires", "BindsTo"}
REVERSE_STOP = {"Requires", "BindsTo", "PartOf", "Requisite"}
# (property, normalized relation, inverse, follow metadata)
PROPERTIES = [
    ("Wants", "Wants", False, True),
    ("Requires", "Requires", False, True),
    ("BindsTo", "BindsTo", False, True),
    ("PartOf", "PartOf", False, False),
    ("RequiredBy", "Requires", True, True),
    ("WantedBy", "Wants", True, False),
    ("BoundBy", "BindsTo", True, True),
    ("ConsistsOf", "PartOf", True, True),
    ("After", "After", False, False),
    ("Before", "Before", False, False),
    ("Conflicts", "Conflicts", False, False),
    ("ConflictedBy", "Conflicts", True, False),
    ("Requisite", "Requisite", False, False),
    ("RequisiteOf", "Requisite", True, True),
    ("Upholds", "Upholds", False, True),
    ("UpheldBy", "Upholds", True, True),
    ("PropagatesStopTo", "PropagatesStopTo", False, True),
    ("StopPropagatedFrom", "PropagatesStopTo", True, False),
]


@dataclass
class Graph:
    edges: set[tuple[UnitKey, str, UnitKey]] = field(default_factory=set)
    inspected: set[UnitKey] = field(default_factory=set)
    aliases: dict[UnitKey, UnitKey] = field(default_factory=dict)
    incomplete: list[str] = field(default_factory=list)
    stale: bool = False

    def canonical(self, key: UnitKey) -> UnitKey:
        return self.aliases.get(key, key)

    def reachable(self, source: UnitKey, mode: str) -> dict[UnitKey, list]:
        source = self.canonical(source)
        paths = {source: []}
        queue = deque([source])
        while queue:
            current = queue.popleft()
            for raw_from, relation, raw_to in sorted(self.edges):
                before, after = self.canonical(raw_from), self.canonical(raw_to)
                next_key = None
                if mode == "stop":
                    if relation in REVERSE_STOP and current == after:
                        next_key = before
                    elif relation == "PropagatesStopTo" and current == before:
                        next_key = after
                elif current == before and relation in (STRONG if mode == "strong" else START):
                    next_key = after
                if next_key is not None and next_key not in paths:
                    paths[next_key] = paths[current] + [(before, relation, after)]
                    queue.append(next_key)
        paths.pop(source, None)
        return paths


def inspect(config: Config, group: str, action: str, approved: set[str], graph: Graph) -> dict:
    steps = config.expand(group)
    raw = {
        systemd_key(config.workloads[key]): index
        for index, step in enumerate(steps)
        for key in step.workloads
        if systemd_key(config.workloads[key]) is not None
    }
    places = {graph.canonical(key): index for key, index in raw.items()}
    report = dict(
        action=action,
        stale=graph.stale,
        diagnostics=[],
        inspected_units=len(graph.inspected),
        requested_units=[key.json() for key in raw],
        uninspected_units=[
            key.json() for key in raw if graph.canonical(key) not in graph.inspected
        ],
        incomplete=list(graph.incomplete),
    )

    def add(level, code, message, units, path=()):
        diagnostic = dict(
            level=level,
            code=code,
            message=message,
            units=[key.json() for key in units],
            path=[f"{a.text()} --{relation}--> {b.text()}" for a, relation, b in path],
        )
        if diagnostic not in report["diagnostics"]:
            report["diagnostics"].append(diagnostic)

    if report["uninspected_units"]:
        report["incomplete"].append("Selected units not fully inspected")
    if any(
        config.workloads[key]["backend"] == "docker" for step in steps for key in step.workloads
    ):
        report["incomplete"].append(
            "Docker lifecycle dependencies are not inspected; children are outside Steps"
        )
    if len(raw) != len(places):
        add("error", "duplicate_alias", "Multiple Workloads resolve to one unit", list(raw))
    if action != "stop":
        registered = {
            graph.canonical(key): logical
            for logical, work in config.workloads.items()
            if (key := systemd_key(work)) is not None
        }
        for source, position in places.items():
            hard = graph.reachable(source, "strong")
            for target, path in graph.reachable(source, "start").items():
                if target not in places:
                    gates = sorted(
                        gate
                        for gate, key in config.manual_followers()
                        if key == registered.get(target) and gate not in approved
                    )
                    context = (
                        f"; surrounding unapproved Manual Gates: {', '.join(gates)}"
                        if gates
                        else ""
                    )
                    add(
                        "warning",
                        "outside_start",
                        f"Starting {source.text()} may request {target.text()} outside the selected Group{context}",
                        [source, target],
                        path,
                    )
                later = places.get(target, -1)
                if later > position:
                    for step in steps[position + 1 : later + 1]:
                        if step.kind != "gate":
                            continue
                        gate = config.gates[step.value]
                        if (
                            gate["type"] == "manual"
                            and step.value in approved
                            and action != "reinitialize"
                        ):
                            continue
                        add(
                            "error" if target in hard else "warning",
                            "gate_bypass",
                            f"Starting {source.text()} requests {target.text()} before Gate {step.value}; functional readiness is not predicted",
                            [source, target],
                            hard.get(target, path),
                        )
        for before, relation, after in sorted(graph.edges):
            before, after = graph.canonical(before), graph.canonical(after)
            if relation == "Conflicts":
                if before in places and after in places:
                    add(
                        "error",
                        "conflicts",
                        f"Configured coexisting units conflict: {before.text()} / {after.text()}",
                        [before, after],
                    )
                elif before in places or after in places:
                    add(
                        "warning",
                        "outside_conflict",
                        "Start may stop a conflicting unit outside the selected Group",
                        [before, after],
                    )
            if relation in ("After", "Before") and before in places and after in places:
                first, second = (after, before) if relation == "After" else (before, after)
                if places[first] > places[second]:
                    add(
                        "warning",
                        "ordering",
                        f"Steps differ from {relation} ordering between {before.text()} and {after.text()}; ordering alone creates no start request",
                        [before, after],
                    )
            if (
                relation == "Requisite"
                and before in places
                and places.get(after, -1) > places[before]
            ):
                add(
                    "warning",
                    "requisite",
                    "Requisite expects an already-running later Workload; it does not start it",
                    [before, after],
                )
    if action in ("stop", "restart", "reinitialize"):
        for source in places:
            for target, path in graph.reachable(source, "stop").items():
                if target not in places:
                    add(
                        "warning",
                        "outside_stop",
                        f"Stopping {source.text()} may affect {target.text()} outside the selected Group",
                        [source, target],
                        path,
                    )
        for source, relation, target in graph.edges:
            if relation == "Upholds" and graph.canonical(target) in places:
                add(
                    "warning",
                    "upholds",
                    "systemd Upholds may restart a stopped Workload",
                    [source, target],
                )
    return report
