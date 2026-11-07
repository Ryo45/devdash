"""Small validated configuration model. Runtime children are never sequence Steps."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

import yaml


class ConfigError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def fields(value: Any, allowed: set[str], context: str) -> dict:
    require(isinstance(value, dict), f"{context} must be a mapping")
    require(not (set(value) - allowed), f"Unknown {context} fields: {set(value) - allowed}")
    return value


def positive(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


@dataclass(frozen=True, order=True)
class UnitKey:
    scope: str
    unit: str

    def text(self) -> str:
        return f"{self.scope}:{self.unit}"

    def json(self) -> dict:
        return {"scope": self.scope, "unit": self.unit}


@dataclass(frozen=True)
class Step:
    kind: str
    value: str | tuple[str, ...]

    @property
    def label(self) -> str:
        return "parallel: " + ", ".join(self.value) if self.kind == "parallel" else str(self.value)

    @property
    def workloads(self) -> tuple[str, ...]:
        if self.kind == "workload":
            return (str(self.value),)
        return self.value if self.kind == "parallel" else ()

    def json(self) -> dict:
        return {self.kind: list(self.value) if self.kind == "parallel" else self.value}


class Config:
    def __init__(self, raw: dict):
        raw = copy.deepcopy(fields(raw, {"workloads", "groups", "gates"}, "config"))
        self.workloads = raw.get("workloads", {})
        self.groups = raw.get("groups", {})
        self.gates = raw.get("gates", {})
        for name, value in (
            ("workloads", self.workloads),
            ("groups", self.groups),
            ("gates", self.gates),
        ):
            require(isinstance(value, dict), f"{name} must be a mapping")
            require(all(isinstance(key, str) and key for key in value), f"Invalid {name} ID")
        references = set()
        for key, work in self.workloads.items():
            fields(
                work,
                {"name", "backend", "scope", "unit", "docker", "controller"},
                f"workload {key}",
            )
            work.setdefault("name", "")
            work.setdefault("scope", "user")
            work.setdefault("unit", "")
            require(isinstance(work["name"], str), f"Invalid display name {key}")
            require(work["scope"] in ("user", "system"), f"Invalid scope {key}")
            if work.get("backend") == "systemd":
                unit = work["unit"]
                require(
                    isinstance(unit, str)
                    and unit.endswith(".service")
                    and "/" not in unit
                    and not any(c.isspace() for c in unit)
                    and not work.get("docker"),
                    f"Invalid systemd reference {key}",
                )
                identity = ("systemd", work["scope"], unit)
            elif work.get("backend") == "docker":
                require(
                    not work["unit"] and work["scope"] == "user",
                    f"Docker {key} does not use unit/scope",
                )
                docker = fields(
                    work.get("docker"), {"socket", "container", "compose"}, f"Docker {key}"
                )
                docker.setdefault("socket", "/var/run/docker.sock")
                require(
                    isinstance(docker["socket"], str) and Path(docker["socket"]).is_absolute(),
                    f"Docker socket must be absolute: {key}",
                )
                require(
                    bool(docker.get("container")) != bool(docker.get("compose")),
                    f"Choose one Docker selector: {key}",
                )
                if docker.get("compose"):
                    compose = fields(docker["compose"], {"project", "service"}, "Compose selector")
                    require(
                        all(
                            isinstance(compose.get(n), str) and compose[n]
                            for n in ("project", "service")
                        ),
                        "Missing Compose project/service",
                    )
                    selector = (compose["project"], compose["service"])
                else:
                    name = docker["container"]
                    require(
                        isinstance(name, str)
                        and name
                        and "/" not in name
                        and not any(c.isspace() for c in name),
                        "Invalid container name",
                    )
                    selector = name
                identity = ("docker", docker["socket"], selector)
            else:
                raise ConfigError(f"Unsupported backend for {key}")
            require(identity not in references, f"Duplicate backend reference: {key}")
            references.add(identity)
            if work.get("controller"):
                controller = fields(work["controller"], {"kind", "socket", "python"}, "controller")
                require(
                    controller.get("kind") in ("supervisord", "roslaunch"), "Unknown controller"
                )
                require(
                    isinstance(controller.get("socket"), str)
                    and Path(controller["socket"]).is_absolute(),
                    "Controller socket must be absolute",
                )
                controller.setdefault("python", "python3")
                require(
                    isinstance(controller["python"], str) and controller["python"],
                    "Invalid controller Python executable",
                )
        self.steps: dict[str, list[Step]] = {}
        used: dict[str, set[str]] = {"workload": set(), "group": set(), "gate": set()}
        for key, group in self.groups.items():
            fields(group, {"name", "steps"}, f"group {key}")
            group.setdefault("name", "")
            require(isinstance(group.get("steps"), list), f"Group {key} needs ordered steps")
            steps = []
            for raw_step in group["steps"]:
                require(isinstance(raw_step, dict) and len(raw_step) == 1, f"Invalid Step in {key}")
                kind, value = next(iter(raw_step.items()))
                require(kind in ("workload", "group", "parallel", "gate"), f"Unknown Step {kind}")
                values = value if kind == "parallel" else [value]
                require(
                    isinstance(values, list)
                    and values
                    and all(isinstance(v, str) and v for v in values),
                    f"Invalid {kind} Step",
                )
                reference_kind = "workload" if kind == "parallel" else kind
                table = {"workload": self.workloads, "group": self.groups, "gate": self.gates}[
                    reference_kind
                ]
                for ref in values:
                    require(ref in table, f"Unknown {reference_kind}: {ref}")
                    require(
                        ref not in used[reference_kind],
                        f"Duplicate {reference_kind} placement: {ref}",
                    )
                    used[reference_kind].add(ref)
                steps.append(Step(kind, tuple(value) if kind == "parallel" else value))
            self.steps[key] = steps
        visited: set[str] = set()

        def visit(key: str, active: set[str]) -> None:
            require(key not in active, f"Group cycle at {key}")
            if key in visited:
                return
            for step in self.steps[key]:
                if step.kind == "group":
                    visit(str(step.value), active | {key})
            visited.add(key)

        for key in self.groups:
            visit(key, set())
        for key, gate in self.gates.items():
            kind = gate.get("type") if isinstance(gate, dict) else None
            allowed = {
                "manual": {"type"},
                "http": {"type", "url", "timeout_ms", "interval_ms", "expected_status"},
                "container_ready": {"type", "workload", "timeout_ms", "interval_ms"},
            }
            require(isinstance(kind, str) and kind in allowed, f"Unknown gate type {key}")
            fields(gate, allowed[kind], f"gate {key}")
            if kind != "manual":
                require(
                    positive(gate.get("timeout_ms")) and positive(gate.get("interval_ms")),
                    f"Invalid gate timing {key}",
                )
            if kind == "http":
                require(
                    isinstance(gate.get("url"), str)
                    and urlparse(gate["url"]).scheme in ("http", "https"),
                    f"Invalid HTTP gate {key}",
                )
                gate.setdefault("expected_status", 200)
                require(
                    isinstance(gate["expected_status"], int)
                    and 100 <= gate["expected_status"] < 600,
                    f"Invalid HTTP status {key}",
                )
            elif kind == "container_ready":
                require(
                    isinstance(gate.get("workload"), str)
                    and self.workloads.get(gate["workload"], {}).get("backend") == "docker",
                    f"Invalid container readiness reference {key}",
                )

    @classmethod
    def load(cls, path: Path) -> Config:
        return cls.parse(path.read_text())

    @classmethod
    def parse(cls, text: str) -> Config:
        # Reject duplicate YAML keys instead of silently replacing a service/group.
        class Loader(yaml.SafeLoader):
            pass

        def mapping(loader, node):
            result = {}
            for key, value in node.value:
                key = loader.construct_object(key, deep=True)
                require(
                    isinstance(key, str) and key not in result, f"Duplicate/invalid YAML key: {key}"
                )
                result[key] = loader.construct_object(value, deep=True)
            return result

        Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
        return cls(yaml.load(text, Loader=Loader))

    def json(self) -> dict:
        return copy.deepcopy(
            {"workloads": self.workloads, "groups": self.groups, "gates": self.gates}
        )

    def expand(self, group: str, reverse: bool = False) -> list[Step]:
        require(group in self.groups, f"Unknown group {group}")
        result = []
        for step in reversed(self.steps[group]) if reverse else self.steps[group]:
            result.extend(self.expand(str(step.value), reverse) if step.kind == "group" else [step])
        return result

    def manual_followers(self) -> set[tuple[str, str]]:
        children = {
            str(s.value) for steps in self.steps.values() for s in steps if s.kind == "group"
        }
        result = set()
        for root in self.groups.keys() - children:
            gates = []
            for step in self.expand(root):
                if step.kind == "gate" and self.gates[step.value]["type"] == "manual":
                    gates.append(str(step.value))
                for workload in step.workloads:
                    result.update((gate, workload) for gate in gates)
        return result

    def import_unit(self, unit: str, scope: str) -> str:
        require(scope in ("user", "system"), "Invalid manager scope")
        require(
            not any(
                w.get("backend") == "systemd" and w["unit"] == unit and w["scope"] == scope
                for w in self.workloads.values()
            ),
            "Unit already imported",
        )
        name = unit.removesuffix(".service")
        key = name if name not in self.workloads else f"{scope}:{name}"
        require(key not in self.workloads, "Logical ID already exists")
        raw = self.json()
        raw["workloads"][key] = {"name": key, "backend": "systemd", "scope": scope, "unit": unit}
        checked = Config(raw)
        self.__dict__.update(checked.__dict__)
        return key


def systemd_key(workload: dict) -> UnitKey | None:
    return (
        UnitKey(workload["scope"], workload["unit"]) if workload["backend"] == "systemd" else None
    )


def observation(**values: Any) -> dict:
    result = dict(
        state="unknown",
        sub_state="",
        pid=None,
        memory_bytes=None,
        cpu_nsec=None,
        canonical_unit=None,
        invocation_id=None,
        manager_generation=None,
        health=None,
        children=[],
        children_error=None,
        error=None,
    )
    result.update(values)
    return result


def step_workloads(steps: list[Step]) -> Iterator[str]:
    return (key for step in steps for key in step.workloads)
