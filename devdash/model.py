"""Workloads, ordered developer Stages and explicit systemd prerequisites."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from .self_protection import application_unit


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


def validate_docker(value, key):
    docker = fields(value, {"socket", "container", "compose"}, f"Docker {key}")
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
            all(isinstance(compose.get(n), str) and compose[n] for n in ("project", "service")),
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
    return (docker["socket"], selector)


def parse_yaml(text):
    # Reject duplicate YAML keys instead of silently replacing a Workload/Stage.
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
    try:
        return yaml.load(text, Loader=Loader)
    except yaml.YAMLError as error:
        raise ConfigError(f"Invalid YAML: {error}") from error


class Config:
    def __init__(self, raw: dict):
        if isinstance(raw, dict) and ("groups" in raw or "gates" in raw):
            raise ConfigError("Legacy Coordinator config: use devdash migrate to propose Stages")
        raw = copy.deepcopy(
            fields(raw, {"workloads", "stages", "dependencies", "plan_templates"}, "config")
        )
        self.workloads = raw.get("workloads", {})
        self.stages = raw.get("stages", [])
        self.dependencies = raw.get("dependencies", {})
        self.plan_templates = raw.get("plan_templates", {})
        require(isinstance(self.workloads, dict), "workloads must be a mapping")
        require(all(isinstance(key, str) and key for key in self.workloads), "Invalid Workload ID")
        references = set()
        for key, work in self.workloads.items():
            fields(
                work,
                {"name", "backend", "scope", "unit", "docker", "controller", "observer"},
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
                    and bool(re.fullmatch(r"[A-Za-z0-9_:@.\\-]+\.service", unit))
                    and "/" not in unit
                    and not any(c.isspace() for c in unit)
                    and not work.get("docker"),
                    f"Invalid systemd reference {key}",
                )
                require(not application_unit(unit), f"devdash cannot manage itself: {unit}")
                identity = ("systemd", work["scope"], unit)
            elif work.get("backend") == "docker":
                require(
                    not work["unit"] and work["scope"] == "user",
                    f"Docker {key} does not use unit/scope",
                )
                identity = ("docker", *validate_docker(work.get("docker"), key))
            else:
                raise ConfigError(f"Unsupported backend for {key}")
            if "observer" in work:
                observer = fields(work["observer"], {"docker", "http"}, f"Observer {key}")
                require(bool(observer), "Observer needs docker or http")
                if "docker" in observer:
                    require(
                        work["backend"] == "systemd", "Docker lifecycle already observes Docker"
                    )
                    observed_identity = ("docker", *validate_docker(observer["docker"], key))
                    require(
                        observed_identity not in references, f"Duplicate Docker ownership: {key}"
                    )
                    references.add(observed_identity)
                if "http" in observer:
                    http = fields(
                        observer["http"], {"url", "expected_status", "timeout_ms"}, "HTTP observer"
                    )
                    require(
                        isinstance(http.get("url"), str)
                        and urlparse(http["url"]).scheme in ("http", "https"),
                        "Invalid HTTP URL",
                    )
                    http.setdefault("expected_status", 200)
                    http.setdefault("timeout_ms", 1500)
                    require(
                        type(http["expected_status"]) is int
                        and 100 <= http["expected_status"] < 600,
                        "Invalid HTTP status",
                    )
                    require(
                        positive(http["timeout_ms"]) and http["timeout_ms"] <= 2500,
                        "HTTP timeout must be 1..2500ms",
                    )
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
        require(isinstance(self.stages, list), "stages must be an ordered list")
        self.membership = {}
        ids = set()
        for index, stage in enumerate(self.stages):
            fields(stage, {"id", "name", "description", "checks", "workloads"}, "Stage")
            key = stage.get("id")
            require(
                isinstance(key, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]+", key)),
                "Invalid Stage ID",
            )
            require(key not in ids, f"Duplicate Stage: {key}")
            ids.add(key)
            stage.setdefault("name", key)
            stage.setdefault("description", "")
            stage.setdefault("checks", [])
            require(
                isinstance(stage["name"], str) and isinstance(stage["description"], str),
                "Invalid Stage text",
            )
            require(
                isinstance(stage["checks"], list)
                and all(isinstance(v, str) for v in stage["checks"]),
                "Invalid Stage checks",
            )
            members = stage.get("workloads")
            require(isinstance(members, list), "Stage needs workloads list")
            for member in members:
                require(
                    isinstance(member, str) and member in self.workloads,
                    f"Unknown Workload: {member}",
                )
                require(member not in self.membership, f"Duplicate Workload placement: {member}")
                self.membership[member] = index
        require(
            isinstance(self.dependencies, dict),
            "dependencies must map Workload IDs to prerequisites",
        )
        for key, prerequisites in self.dependencies.items():
            require(key in self.workloads, f"Unknown dependent: {key}")
            require(
                isinstance(prerequisites, list)
                and all(isinstance(p, str) and p in self.workloads for p in prerequisites),
                f"Invalid dependencies: {key}",
            )
            require(len(prerequisites) == len(set(prerequisites)), f"Duplicate prerequisite: {key}")
        visited = set()

        def visit(key, active):
            require(key not in active, f"Dependency cycle: {key}")
            if key in visited:
                return
            for prerequisite in self.dependencies.get(key, []):
                visit(prerequisite, active | {key})
            visited.add(key)

        for key in self.workloads:
            visit(key, set())
        for key, prerequisites in self.dependencies.items():
            for prerequisite in prerequisites:
                work, before = self.workloads[key], self.workloads[prerequisite]
                require(
                    work["backend"] == before["backend"] == "systemd",
                    "Dependencies require systemd Workloads",
                )
                require(
                    work["scope"] == before["scope"],
                    "Cross-manager dependencies cannot be generated",
                )
                require(
                    key in self.membership and prerequisite in self.membership,
                    "Dependent Workloads must belong to Stages",
                )
                require(
                    self.membership[prerequisite] < self.membership[key],
                    f"Prerequisite {prerequisite} must be in an earlier Stage than {key}; same/reverse Stage dependency unsupported",
                )

        require(isinstance(self.plan_templates, dict), "plan_templates must be a mapping")
        for key, template in self.plan_templates.items():
            require(isinstance(key, str) and key, "Invalid Plan Template ID")
            fields(template, {"kind", "from_stage", "name"}, "Plan Template")
            require(
                template.get("kind") in ("startup", "start_missing", "restart_from", "stop_from"),
                "Unknown Plan Template kind",
            )
            require(isinstance(template.get("name", key), str), "Invalid Template name")
            if template["kind"] in ("startup", "start_missing"):
                require("from_stage" not in template, "Startup Template uses all Stages")
            else:
                self.stage(template.get("from_stage"))

    def stage(self, key):
        result = next((stage for stage in self.stages if stage["id"] == key), None)
        require(result is not None, f"Unknown Stage: {key}")
        return result

    def next_stage(self, after):
        index = self.stages.index(self.stage(after)) + 1
        require(index < len(self.stages), "No next Stage")
        return self.stages[index]

    @classmethod
    def load(cls, path: Path) -> Config:
        return cls.parse(path.read_text())

    @classmethod
    def parse(cls, text: str) -> Config:
        return cls(parse_yaml(text))

    def json(self) -> dict:
        return copy.deepcopy(
            dict(
                workloads=self.workloads,
                stages=self.stages,
                dependencies=self.dependencies,
                plan_templates=self.plan_templates,
            )
        )

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


def docker_reference(work: dict) -> dict | None:
    """Internal observation selector; never selects a Lifecycle operation path."""
    return (
        work.get("docker")
        if work.get("backend") == "docker"
        else work.get("observer", {}).get("docker")
    )


def readiness(state="unknown", source="unavailable", detail="Not observed") -> dict:
    return dict(state=state, source=source, detail=detail)


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
        http=None,
        readiness=readiness(),
        internal=None,
        observer_error=None,
        children=[],
        children_error=None,
        controller=None,
        error=None,
    )
    result.update(values)
    return result
