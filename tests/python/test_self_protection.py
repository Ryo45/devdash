"""Application lifecycle is outside Workload management."""

import asyncio

import pytest
import yaml
from private_bus import Harness

from devdash.backend import Backends
from devdash.ipc import Server, request
from devdash.model import Config, ConfigError
from devdash.self_protection import SelfManagementError, application_command


@pytest.mark.parametrize("scope", ["user", "system"])
@pytest.mark.parametrize(
    "unit",
    [
        "devdash.service",
        "devdash-core.service",
        "devdash-gui.service",
        "devdash-core@robot.service",
        "devdash-gui@seat0.service",
    ],
)
def test_reserved_units_rejected_by_config_and_import(scope, unit):
    with pytest.raises(ConfigError, match="cannot manage itself"):
        Config({"workloads": {"self": {"backend": "systemd", "scope": scope, "unit": unit}}})
    config = Config({})
    with pytest.raises(ConfigError, match="cannot manage itself"):
        config.import_unit(unit, scope)
    assert config.workloads == {}


def test_application_logical_id_and_ros_bridge_are_not_reserved():
    config = Config(
        {
            "workloads": {
                "devdash-core": {"backend": "systemd", "unit": "camera.service"},
                "bridge": {"backend": "systemd", "unit": "devdash-roslaunch.service"},
            }
        }
    )
    assert set(config.workloads) == {"devdash-core", "bridge"}


@pytest.mark.parametrize(
    "executable,arguments,expected",
    [
        ("/venv/bin/devdash", ["devdash", "daemon"], True),
        ("/venv/bin/devdash-gui", ["devdash-gui"], True),
        ("/usr/bin/python3", ["python3", "-I", "-m", "devdash", "daemon"], True),
        (
            "/usr/bin/python3.12",
            ["python3.12", "-X", "dev", "-W", "error", "-m", "devdash.gui"],
            True,
        ),
        ("/usr/bin/python3", ["python3", "-mdevdash.gui"], True),
        ("/usr/bin/python3", ["python3", "/venv/bin/devdash-gui"], True),
        ("/usr/bin/python3", ["python3", "/src/devdash/__main__.py", "daemon"], True),
        ("/usr/bin/python3", ["python3", "-m", "devdash_roslaunch"], False),
        ("/venv/bin/devdash-roslaunch", ["devdash-roslaunch"], False),
        ("/bin/echo", ["echo", "-m", "devdash"], False),
        ("/usr/bin/python3", ["python3", "-c", "print(1)", "-m", "devdash"], False),
        ("/usr/bin/python3", ["python3", "/robot/worker.py", "-m", "devdash"], False),
    ],
)
def test_entrypoint_detection_uses_commands_not_arbitrary_arguments(
    executable, arguments, expected
):
    assert application_command(executable, arguments) == expected


@pytest.fixture
async def own_units(tmp_path, monkeypatch):
    harness = Harness(tmp_path, monkeypatch)
    try:
        await harness.start(
            own_units={"user": "custom-core.service", "system": "user@1000.service"}
        )
        yield harness
    finally:
        await harness.close()


@pytest.mark.parametrize(
    "scope,name", [("user", "custom-core.service"), ("system", "user@1000.service")]
)
@pytest.mark.parametrize("action", ["start", "stop", "restart"])
async def test_pid_resolved_current_unit_and_containing_manager_never_receive_jobs(
    own_units, scope, name, action
):
    h = own_units
    with pytest.raises(SelfManagementError, match="containing unit"):
        await h.backend.operate({"scope": scope, "unit": name}, action)
    assert all(not manager.calls for manager in h.managers.values())


async def test_alias_of_renamed_core_is_refused(own_units):
    h = own_units
    h.managers["user"].aliases["innocent-alias.service"] = "custom-core.service"
    with pytest.raises(SelfManagementError):
        await h.backend.validate_registration({"scope": "user", "unit": "innocent-alias.service"})
    with pytest.raises(SelfManagementError):
        await h.backend.operate({"scope": "user", "unit": "innocent-alias.service"}, "stop")
    assert not h.managers["user"].calls


def exec_row(executable, arguments):
    return [executable, arguments, False, 0, 0, 0, 0, 0, 0, 0]


@pytest.mark.parametrize(
    "arguments", [["python3", "-I", "-m", "devdash", "daemon"], ["python3", "-m", "devdash.gui"]]
)
async def test_stopped_renamed_application_is_refused_by_execstart(own_units, arguments):
    h = own_units
    h.managers["user"].exec_start["renamed.service"] = [exec_row("/usr/bin/python3", arguments)]
    with pytest.raises(SelfManagementError):
        await h.backend.validate_registration({"scope": "user", "unit": "renamed.service"})
    with pytest.raises(SelfManagementError):
        await h.backend.operate({"scope": "user", "unit": "renamed.service"}, "start")
    assert not h.managers["user"].calls


async def test_startup_rejects_resolved_self_but_preserves_unavailable_workloads(own_units):
    from unittest.mock import AsyncMock

    h = own_units
    backend = Backends(h.backend.config)
    backend.systemd = h.backend
    original = h.backend.start
    h.backend.start = AsyncMock()  # Already connected to the real private buses.
    try:
        h.backend.config = Config(
            {"workloads": {"self": {"backend": "systemd", "unit": "custom-core.service"}}}
        )
        with pytest.raises(SelfManagementError):
            await backend.start()
        h.backend.config = Config(
            {"workloads": {"ordinary": {"backend": "systemd", "unit": "unavailable.service"}}}
        )
        backend.validate_registration = AsyncMock(side_effect=RuntimeError("manager unavailable"))
        await backend.start()
        assert "ordinary" in h.backend.config.workloads
    finally:
        h.backend.start = original


async def test_scan_filters_own_unit_aliases_and_reserved_names(own_units):
    h = own_units
    manager = h.managers["user"]
    manager.aliases.update(
        {"alias.service": "custom-core.service", "devdash-gui.service": "some-gui.service"}
    )
    units = await h.backend.scan("user")
    assert "same.service" in units
    assert "custom-core.service" not in units and "alias.service" not in units
    assert "devdash-gui.service" not in units


async def test_import_refusal_leaves_config_and_backup_untouched(own_units, tmp_path):
    h = own_units
    manager = h.managers["user"]
    manager.exec_start["ordinary-new.service"] = []
    manager.exec_start["custom-gui.service"] = [exec_row("/venv/bin/devdash-gui", ["devdash-gui"])]
    path, socket = tmp_path / "config.yaml", tmp_path / "core.sock"
    path.write_text(yaml.safe_dump(h.engine.config.json()))
    original = path.read_text()
    server = Server(h.engine, path)
    listener = await asyncio.start_unix_server(server.handle, path=str(socket))
    try:
        for unit in ("devdash-core.service", "custom-core.service", "custom-gui.service"):
            with pytest.raises(RuntimeError, match="cannot manage itself|not available"):
                await request(
                    socket,
                    {"command": "import", "scope": "user", "units": ["ordinary-new.service", unit]},
                )
            assert path.read_text() == original
            assert not path.with_suffix(".yaml.bak").exists()
            assert unit not in [work["unit"] for work in h.engine.config.workloads.values()]
        assert not manager.calls
    finally:
        listener.close()
        await listener.wait_closed()
        await asyncio.gather(*server.clients, return_exceptions=True)


async def test_regular_service_and_ros_bridge_remain_operable(own_units):
    h = own_units
    manager = h.managers["user"]
    manager.exec_start["bridge.service"] = [
        exec_row("/usr/bin/python3", ["python3", "-m", "devdash_roslaunch"])
    ]
    await h.backend.operate({"scope": "user", "unit": "bridge.service"}, "start")
    await h.backend.operate({"scope": "user", "unit": "same.service"}, "restart")
    assert manager.calls == [("StartUnit", "bridge.service"), ("RestartUnit", "same.service")]


async def test_operation_rechecks_changed_execstart_after_registration(own_units):
    h = own_units
    manager = h.managers["user"]
    manager.exec_start["changed.service"] = [exec_row("/bin/sleep", ["sleep", "3600"])]
    work = {"scope": "user", "unit": "changed.service"}
    await h.backend.validate_registration(work)
    manager.exec_start["changed.service"] = [
        exec_row("/usr/bin/python3", ["python3", "-I", "-m", "devdash", "daemon"])
    ]
    with pytest.raises(SelfManagementError):
        await h.backend.operate(work, "stop")
    assert not manager.calls
