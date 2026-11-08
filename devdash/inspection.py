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
                elif current == before and relation in (
                    (STRONG | {"Requisite"})
                    if mode == "required"
                    else STRONG
                    if mode == "strong"
                    else START
                ):
                    next_key = after
                if next_key is not None and next_key not in paths:
                    paths[next_key] = paths[current] + [(before, relation, after)]
                    queue.append(next_key)
        paths.pop(source, None)
        return paths


def inspect(config: Config, stage: str, action: str, graph: Graph, check_declared=True) -> dict:
    members = config.stage(stage)["workloads"]
    raw = {
        systemd_key(config.workloads[key])
        for key in members
        if systemd_key(config.workloads[key]) is not None
    }
    selected = {graph.canonical(key) for key in raw}
    positions = {
        graph.canonical(key): config.membership[logical]
        for logical, work in config.workloads.items()
        if logical in config.membership and (key := systemd_key(work)) is not None
    }
    report = dict(
        action=action,
        stale=graph.stale,
        complete=not graph.incomplete
        and all(graph.canonical(key) in graph.inspected for key in raw),
        diagnostics=[],
        inspected_units=len(graph.inspected),
        requested_units=[key.json() for key in sorted(raw)],
        uninspected_units=[
            key.json() for key in sorted(raw) if graph.canonical(key) not in graph.inspected
        ],
        incomplete=list(graph.incomplete),
    )

    def add(level, code, message, units, path=()):
        item = dict(
            level=level,
            code=code,
            message=message,
            units=[key.json() for key in units],
            path=[f"{a.text()} --{relation}--> {b.text()}" for a, relation, b in path],
        )
        if item not in report["diagnostics"]:
            report["diagnostics"].append(item)

    if report["uninspected_units"]:
        report["incomplete"].append("Selected systemd units not fully inspected")
    if any(config.workloads[key]["backend"] == "docker" for key in members):
        report["incomplete"].append(
            "Docker dependency inspection is unavailable; only explicit lifecycle requests are tracked"
        )
    registered = [
        systemd_key(work) for work in config.workloads.values() if systemd_key(work) is not None
    ]
    if len(raw) != len(selected) or any(
        sum(graph.canonical(key) == unit for key in registered) > 1 for unit in selected
    ):
        add("error", "duplicate_alias", "Workloads resolve to the same unit", sorted(raw))
    edges = {(graph.canonical(a), relation, graph.canonical(b)) for a, relation, b in graph.edges}
    if action != "stop":
        for source in selected:
            for target, path in graph.reachable(source, "start").items():
                if target not in selected:
                    add(
                        "error"
                        if target in positions
                        and positions[target] > positions.get(source, -1)
                        and target in graph.reachable(source, "strong")
                        else "warning",
                        "outside_start",
                        f"systemd may start {target.text()} outside selected Stage from {source.text()}; Next is not an enforcement boundary",
                        [source, target],
                        path,
                    )
            for target, path in graph.reachable(source, "required").items():
                if (
                    target in positions
                    and source in positions
                    and positions[target] >= positions[source]
                ):
                    add(
                        "error",
                        "stage_dependency",
                        "Mandatory dependency is in the same or a later Stage",
                        [source, target],
                        path,
                    )
        # A small metadata cycle check, never an execution graph.
        visiting, visited = set(), set()

        def cycle(node):
            if node in visiting:
                return True
            if node in visited:
                return False
            visiting.add(node)
            if any(cycle(b) for a, rel, b in edges if a == node and rel in STRONG | {"Requisite"}):
                return True
            visiting.remove(node)
            visited.add(node)
            return False

        if any(cycle(node) for node in selected):
            add(
                "error",
                "dependency_cycle",
                "Existing mandatory systemd dependencies contain a cycle",
                sorted(selected),
            )
        for before, relation, after in edges:
            if relation == "Conflicts" and (before in selected or after in selected):
                add(
                    "error" if before in positions and after in positions else "warning",
                    "conflicts",
                    "systemd Conflicts may stop a Workload intended to coexist",
                    [before, after],
                )
            if (
                relation in ("After", "Before")
                and before in positions
                and after in positions
                and (before in selected or after in selected)
            ):
                dependent, prerequisite = (
                    (before, after) if relation == "After" else (after, before)
                )
                if positions[prerequisite] >= positions[dependent]:
                    add(
                        "error",
                        "ordering",
                        "systemd ordering conflicts with independent, forwards Stages; ordering alone does not start units",
                        [before, after],
                    )
        for key in members if check_declared else []:
            source = systemd_key(config.workloads[key])
            for dependency in config.dependencies.get(key, []):
                target = systemd_key(config.workloads[dependency])
                for relation in ("Requisite", "After"):
                    if (graph.canonical(source), relation, graph.canonical(target)) not in edges:
                        add(
                            "error",
                            "unapplied_dependency",
                            f"Declared {relation} dependency is not present in manager; review/apply generated drop-in and reload systemd explicitly",
                            [source, target],
                        )
        declared = {
            (
                graph.canonical(systemd_key(config.workloads[source])),
                graph.canonical(systemd_key(config.workloads[target])),
            )
            for source, refs in config.dependencies.items()
            for target in refs
        }
        for source, relation, target in edges:
            if (
                source in selected
                and relation in STRONG | {"Requisite"}
                and (source, target) not in declared
                and (target in positions or relation == "Requisite")
            ):
                add(
                    "warning",
                    "existing_dependency",
                    "Existing unit prerequisite is outside the generator's declared dependencies; it is retained, not overwritten",
                    [source, target],
                )
    if action in ("stop", "restart"):
        for source in selected:
            for target, path in graph.reachable(source, "stop").items():
                if target not in selected:
                    add(
                        "warning",
                        "outside_stop",
                        f"systemd may stop {target.text()} outside selected Stage",
                        [source, target],
                        path,
                    )
        for source, relation, target in edges:
            if relation == "Upholds" and target in selected:
                add(
                    "warning",
                    "upholds",
                    "systemd Upholds may restart a stopped Workload",
                    [source, target],
                )
    return report
