"""Diagnostic metadata graph only; never used to schedule Stage members."""

import pytest

from devdash.inspection import Graph, inspect
from devdash.model import Config, UnitKey


def graph(config, *edges):
    return Graph(
        inspected={UnitKey(w["scope"], w["unit"]) for w in config.workloads.values()},
        edges={
            (UnitKey("user", a + ".service"), r, UnitKey("user", b + ".service"))
            for a, r, b in edges
        },
    )


@pytest.mark.parametrize(
    "relation,level", [("Requires", "error"), ("BindsTo", "error"), ("Wants", "warning")]
)
def test_start_propagation_to_later_stage(config, relation, level):
    report = inspect(config, "sensors", "start", graph(config, ("camera", relation, "detector")))
    assert any(d["code"] == "outside_start" and d["level"] == level for d in report["diagnostics"])


def test_ordering_alone_never_creates_start_request(config):
    report = inspect(config, "sensors", "start", graph(config, ("camera", "After", "detector")))
    assert [d["code"] for d in report["diagnostics"]] == ["ordering"]


@pytest.mark.parametrize("relation", ["PartOf", "BindsTo", "Requires", "Requisite"])
def test_stop_propagation_is_directional(config, relation):
    data = graph(config, ("planner", relation, "detector"))
    assert any(
        d["code"] == "outside_stop"
        for d in inspect(config, "perception", "stop", data)["diagnostics"]
    )
    assert not any(
        d["code"] == "outside_stop" for d in inspect(config, "control", "stop", data)["diagnostics"]
    )


def test_unregistered_intermediate_is_inspected_not_imported(config):
    data = graph(config, ("camera", "Requires", "bridge"), ("bridge", "Requires", "detector"))
    item = next(
        d
        for d in inspect(config, "sensors", "start", data)["diagnostics"]
        if d["code"] == "outside_start" and d["units"][-1]["unit"] == "detector.service"
    )
    assert item["level"] == "error" and len(item["path"]) == 2 and "bridge" not in config.workloads


def test_conflicts_before_stop_to_and_upholds(config):
    data = graph(config, ("detector", "Before", "camera"), ("camera", "Conflicts", "detector"))
    assert {d["code"] for d in inspect(config, "sensors", "start", data)["diagnostics"]} == {
        "ordering",
        "conflicts",
    }
    data = graph(
        config, ("detector", "PropagatesStopTo", "planner"), ("outside", "Upholds", "detector")
    )
    assert {d["code"] for d in inspect(config, "perception", "stop", data)["diagnostics"]} == {
        "outside_stop",
        "upholds",
    }


def test_cycles_same_stage_and_incomplete_metadata_are_explicit(config):
    data = graph(config, ("camera", "Requires", "imu"), ("imu", "Requires", "camera"))
    data.incomplete.append("permission denied")
    result = inspect(config, "sensors", "start", data)
    assert not result["complete"] and result["incomplete"] == ["permission denied"]
    assert {d["code"] for d in result["diagnostics"]} >= {"stage_dependency", "dependency_cycle"}
    data.inspected.remove(UnitKey("user", "camera.service"))
    assert inspect(config, "sensors", "start", data)["uninspected_units"] == [
        {"scope": "user", "unit": "camera.service"}
    ]


def test_declared_prerequisites_require_live_dropin_but_editor_can_preview(config):
    raw = config.json()
    raw["dependencies"] = {"detector": ["camera", "imu"]}
    config = Config(raw)
    data = graph(config)
    result = inspect(config, "perception", "start", data)
    assert any(d["code"] == "unapplied_dependency" for d in result["diagnostics"])
    assert not inspect(config, "perception", "start", data, check_declared=False)["diagnostics"]
    for target in ("camera", "imu"):
        for relation in ("Requisite", "After"):
            data.edges.add(
                (
                    UnitKey("user", "detector.service"),
                    relation,
                    UnitKey("user", target + ".service"),
                )
            )
    assert not inspect(config, "perception", "start", data)["diagnostics"]


def test_same_name_different_managers_and_alias_collision(config):
    assert config.import_unit("camera.service", "system") != "camera"
    data = graph(config)
    data.aliases[UnitKey("user", "imu.service")] = UnitKey("user", "camera.service")
    assert any(
        d["code"] == "duplicate_alias"
        for d in inspect(config, "sensors", "start", data)["diagnostics"]
    )


def test_alias_duplicate_in_another_stage_is_also_rejected(config):
    raw = config.json()
    raw["workloads"]["alias"] = dict(backend="systemd", unit="camera-alias.service")
    raw["stages"][1]["workloads"].append("alias")
    config = Config(raw)
    data = graph(config)
    data.aliases[UnitKey("user", "camera-alias.service")] = UnitKey("user", "camera.service")
    assert any(
        d["code"] == "duplicate_alias"
        for d in inspect(config, "sensors", "start", data)["diagnostics"]
    )
