"""Determinism, ownership, diff, stale files and atomic publication."""

from pathlib import Path

import pytest

from devdash.generator import generate, preview, read_current, render, save_source
from devdash.model import Config


@pytest.fixture
def dependent(config):
    raw = config.json()
    raw["dependencies"] = {"detector": ["camera", "imu"], "planner": ["detector"]}
    return Config(raw)


def test_generated_dropins_and_runtime_are_from_one_model(dependent, tmp_path):
    files = render(dependent)
    assert Config.parse(files["devdash.yaml"]).json() == dependent.json()
    path = "systemd/user/detector.service.d/60-devdash-stage-dependencies.conf"
    assert "Requisite=camera.service imu.service" in files[path]
    assert "After=camera.service imu.service" in files[path]
    assert "ExecStart" not in files[path] and "Restart=" not in files[path]
    before = preview(dependent, tmp_path)
    assert not list(tmp_path.iterdir())
    first = generate(dependent, tmp_path)
    assert first == before
    assert (tmp_path / "current/devdash.yaml").is_file()
    again = generate(dependent, tmp_path)
    assert again["generation"] == first["generation"]
    assert all(c["action"] == "unchanged" for c in again["changes"])


def test_obsolete_dropins_detected_without_deleting_unrelated_files(dependent, tmp_path):
    generate(dependent, tmp_path)
    unrelated = tmp_path / "my-unit.service"
    unrelated.write_text("not owned")
    raw = dependent.json()
    raw["dependencies"].pop("planner")
    config = Config(raw)
    plan = preview(config, tmp_path)
    assert any(c["action"] == "remove" and "planner" in c["path"] for c in plan["changes"])
    assert any(c["diff"] for c in plan["changes"])
    generate(config, tmp_path)
    assert unrelated.read_text() == "not owned"
    assert not (
        tmp_path / "current/systemd/user/planner.service.d/60-devdash-stage-dependencies.conf"
    ).exists()


@pytest.mark.parametrize("tamper", ["file", "extra", "manifest", "symlink", "current", "store"])
def test_modified_or_unowned_files_are_never_overwritten(dependent, tmp_path, tamper):
    generate(dependent, tmp_path)
    bundle = (tmp_path / "current").resolve()
    if tamper == "file":
        (bundle / "devdash.yaml").write_text("manual change")
    elif tamper == "extra":
        (bundle / "personal.txt").write_text("personal")
    elif tamper == "manifest":
        (bundle / ".manifest.json").write_text("{}")
    elif tamper == "symlink":
        (bundle / "personal").symlink_to("/tmp")
    elif tamper == "current":
        (tmp_path / "current").unlink()
        (tmp_path / "current").write_text("unrelated")
    else:
        (tmp_path / ".devdash-generated/OWNER").write_text("someone else")
    with pytest.raises((ValueError, OSError)):
        generate(dependent, tmp_path)


@pytest.mark.parametrize("failure", ["write", "switch"])
def test_failed_generation_does_not_publish_partial_configuration(
    config, dependent, tmp_path, monkeypatch, failure
):
    first = generate(config, tmp_path)
    original = Path.write_text
    if failure == "write":

        def fail(path, *args, **kwargs):
            if path.suffix == ".conf":
                raise OSError("disk full")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", fail)
    else:
        monkeypatch.setattr(
            "devdash.generator.os.replace",
            lambda *args: (_ for _ in ()).throw(OSError("switch failed")),
        )
    with pytest.raises(OSError):
        generate(dependent, tmp_path)
    name, files = read_current(tmp_path)
    assert (
        name == first["generation"] and Config.parse(files["devdash.yaml"]).json() == config.json()
    )


def test_source_save_requires_matching_original_and_refuses_symlink(config, tmp_path):
    path = tmp_path / "source.yaml"
    original = save_source(path, config)
    path.write_text("changed outside editor")
    with pytest.raises(ValueError, match="changed"):
        save_source(path, config, original)
    assert path.read_text() == "changed outside editor"
    link = tmp_path / "link.yaml"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="symlink"):
        save_source(link, config)


def test_unrelated_output_collision_is_not_claimed(config, tmp_path):
    (tmp_path / "current").mkdir()
    (tmp_path / "current/personal").write_text("mine")
    with pytest.raises(ValueError):
        generate(config, tmp_path)
    assert (tmp_path / "current/personal").read_text() == "mine"


def test_generated_runtime_cannot_be_used_as_editable_import_source(config, tmp_path):
    generate(config, tmp_path)
    path = tmp_path / "current/devdash.yaml"
    before = path.read_text()
    with pytest.raises(ValueError, match="read-only"):
        save_source(path, config, before)
    assert read_current(tmp_path)[1]["devdash.yaml"] == before


def test_edited_historical_generation_cannot_be_republished(config, dependent, tmp_path):
    first = generate(config, tmp_path)
    generate(dependent, tmp_path)
    old = tmp_path / ".devdash-generated" / first["generation"]
    (old / ".manifest.json").write_text("{}")
    with pytest.raises(ValueError, match="manifest"):
        generate(config, tmp_path)
    assert read_current(tmp_path)[1] == render(dependent)
