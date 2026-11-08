import subprocess
import sys

import pytest

from devdash.migration import migrate
from devdash.model import Config

LEGACY = """
workloads:
  camera: {backend: systemd, unit: camera.service}
  imu: {backend: systemd, unit: imu.service}
  detector: {backend: systemd, unit: detector.service}
groups:
  robot:
    steps: [{group: sensors}, {gate: approve}, {workload: detector}]
  sensors:
    steps: [{parallel: [camera, imu]}]
gates:
  approve: {type: manual}
"""


def test_migration_preserves_workloads_parallelism_and_manual_checks_without_latches():
    report = migrate(LEGACY)
    config = Config(report["config"])
    assert config.stages[0]["workloads"] == ["camera", "imu"]
    assert config.stages[1]["workloads"] == ["detector"]
    assert "approve" in config.stages[0]["checks"][0]
    assert config.dependencies == {} and not report["readiness_actions"]
    assert "gates" not in config.json() and report["warnings"]


@pytest.mark.parametrize("kind", ["http", "container_ready"])
def test_readiness_conditions_are_preserved_in_migration_report_and_require_ack(tmp_path, kind):
    extra = "url: http://localhost/ready" if kind == "http" else "workload: detector"
    text = LEGACY.replace(
        "type: manual", f"type: {kind}, {extra}, timeout_ms: 1000, interval_ms: 50"
    )
    source, output = tmp_path / "old.yaml", tmp_path / "new.yaml"
    source.write_text(text)
    report = migrate(text)
    assert report["readiness_actions"][0]["condition"]["type"] == kind
    args = [sys.executable, "-m", "devdash", "migrate", str(source), "--output", str(output)]
    refused = subprocess.run(args, capture_output=True)
    assert refused.returncode != 0 and not output.exists()
    accepted = subprocess.run([*args, "--ack-readiness-delegation"], capture_output=True)
    assert accepted.returncode == 0 and output.with_suffix(".yaml.migration.json").exists()
    assert source.read_text() == text
    assert subprocess.run(args, capture_output=True).returncode != 0


@pytest.mark.parametrize(
    "text",
    [
        LEGACY.replace("{parallel: [camera, imu]}", "{parallel: [camera, camera]}"),
        LEGACY.replace("{group: sensors}", "{group: robot}"),
        LEGACY.replace("{workload: detector}", "{bad: detector}"),
        "workloads: [",
    ],
)
def test_ambiguous_or_invalid_legacy_configs_refused(text):
    with pytest.raises(ValueError):
        migrate(text)


def test_leading_manual_check_and_unused_readiness_definition_are_not_silently_lost():
    text = LEGACY.replace(
        "steps: [{group: sensors}", "steps: [{gate: leading}, {group: sensors}"
    ).replace(
        "gates:\n",
        "gates:\n  leading: {type: manual}\n  unused_http: {type: http, url: http://localhost/health}\n",
    )
    result = migrate(text)
    assert "leading" in result["config"]["stages"][0]["checks"][0]
    assert any(action["gate"] == "unused_http" for action in result["readiness_actions"])
