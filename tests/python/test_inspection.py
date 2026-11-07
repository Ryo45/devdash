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
