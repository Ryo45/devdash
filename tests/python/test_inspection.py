import pytest

from devdash.inspection import Graph, inspect
from devdash.model import UnitKey


def graph(config, *edges):
    result = Graph()
    result.inspected = {UnitKey(w["scope"], w["unit"]) for w in config.workloads.values()}
    for left, kind, right in edges:
        result.edges.add(
            (UnitKey("user", left + ".service"), kind, UnitKey("user", right + ".service"))
        )
    return result


@pytest.mark.parametrize("kind", ["Requires", "Wants"])
def test_gate_bypass(config, kind):
    report = inspect(config, "robot", "start", set(), graph(config, ("camera", kind, "detector")))
    assert any(
        d["level"] == ("error" if kind == "Requires" else "warning") and "Gate" in d["message"]
        for d in report["diagnostics"]
    )


def test_after_is_only_ordering(config):
    report = inspect(
        config, "robot", "start", set(), graph(config, ("camera", "After", "detector"))
    )
    assert not any("Gate" in d["message"] for d in report["diagnostics"])
    assert report["diagnostics"]


@pytest.mark.parametrize("kind", ["PartOf", "BindsTo", "Requires"])
def test_outside_stop(config, kind):
    report = inspect(
        config, "perception", "restart", {"ready"}, graph(config, ("outside", kind, "detector"))
    )
    assert any("outside" in d["message"] for d in report["diagnostics"])


def test_conflict_and_incomplete(config):
    data = graph(config, ("camera", "Conflicts", "detector"))
    data.incomplete.append("budget exhausted")
    report = inspect(config, "robot", "start", set(), data)
    assert any(d["level"] == "error" for d in report["diagnostics"])
    assert "budget exhausted" in report["incomplete"]


@pytest.mark.parametrize("relation", ["Requires", "BindsTo"])
def test_unregistered_intermediate_can_bypass_gate(config, relation):
    data = graph(config, ("camera", relation, "bridge"), ("bridge", relation, "detector"))
    report = inspect(config, "robot", "start", set(), data)
    bypass = next(d for d in report["diagnostics"] if d["code"] == "gate_bypass")
    assert bypass["level"] == "error" and len(bypass["path"]) == 2
    assert any("bridge.service" in path for path in bypass["path"])
    assert "bridge" not in config.workloads


@pytest.mark.parametrize("relation", ["PartOf", "BindsTo", "Requires", "Requisite"])
def test_stop_propagation_is_directional(config, relation):
    data = graph(config, ("planner", relation, "detector"))
    assert any(
        d["code"] == "outside_stop"
        for d in inspect(config, "perception", "stop", set(), data)["diagnostics"]
    )
    raw = config.json()
    raw["groups"]["robot"]["steps"][-1] = {"group": "planning"}
    raw["groups"]["planning"] = {"steps": [{"workload": "planner"}]}
    from devdash.model import Config

    report = inspect(Config(raw), "planning", "stop", set(), data)
    assert not any(d["code"] == "outside_stop" for d in report["diagnostics"])


def test_before_conflicts_and_stop_to_are_distinct(config):
    data = graph(config, ("detector", "Before", "camera"))
    report = inspect(config, "robot", "start", set(), data)
    assert [d["code"] for d in report["diagnostics"]] == ["ordering"]
    data.edges.add(
        (UnitKey("user", "camera.service"), "Conflicts", UnitKey("user", "detector.service"))
    )
    assert any(
        d["level"] == "error" for d in inspect(config, "robot", "start", set(), data)["diagnostics"]
    )
    assert not inspect(config, "robot", "stop", set(), data)["diagnostics"]
    data.edges.add(
        (
            UnitKey("user", "detector.service"),
            "PropagatesStopTo",
            UnitKey("user", "planner.service"),
        )
    )
    assert any(
        d["code"] == "outside_stop"
        for d in inspect(config, "perception", "stop", set(), data)["diagnostics"]
    )


def test_approval_reuse_and_reinitialize_change_gate_analysis(config):
    data = graph(config, ("camera", "Requires", "detector"))
    normal = inspect(config, "robot", "restart", {"ready"}, data)
    reset = inspect(config, "robot", "reinitialize", {"ready"}, data)
    assert not any(d["code"] == "gate_bypass" for d in normal["diagnostics"])
    assert any(d["code"] == "gate_bypass" and d["level"] == "error" for d in reset["diagnostics"])


def test_same_name_different_managers_and_import(config):
    system_id = config.import_unit("camera.service", "system")
    assert system_id != "camera" and config.workloads[system_id]["scope"] == "system"
    data = graph(config)
    data.edges.add(
        (UnitKey("system", "camera.service"), "Requires", UnitKey("user", "detector.service"))
    )
    assert not any(
        d["code"] == "gate_bypass"
        for d in inspect(config, "robot", "start", set(), data)["diagnostics"]
    )
    from devdash.model import Config

    empty = Config({})
    assert empty.import_unit("same.service", "user") != empty.import_unit("same.service", "system")
    Config(empty.json())


def test_partial_inspection_is_explicit_and_cycles_terminate(config):
    data = graph(config, ("camera", "Requires", "imu"), ("imu", "Requires", "camera"))
    data.incomplete.append("permission denied")
    report = inspect(config, "robot", "start", set(), data)
    assert report["incomplete"] == ["permission denied"]
    assert not report["diagnostics"]
    data.inspected.remove(UnitKey("user", "detector.service"))
    report = inspect(config, "robot", "start", set(), data)
    assert report["uninspected_units"] == [{"scope": "user", "unit": "detector.service"}]


def test_partial_group_warns_without_enforcing_parent_gate(config):
    data = graph(config, ("camera", "Requires", "detector"))
    report = inspect(config, "sensors", "start", set(), data)
    outside = next(d for d in report["diagnostics"] if d["code"] == "outside_start")
    assert "ready" in outside["message"]
    assert all(d["level"] == "warning" for d in report["diagnostics"])
    assert any(
        d["level"] == "error" for d in inspect(config, "robot", "start", set(), data)["diagnostics"]
    )
