"""Small shared policy for application units, independent of GUI and D-Bus."""

import re
from pathlib import PurePath

APPLICATION_UNITS = frozenset({"devdash", "devdash-core", "devdash-gui"})
APPLICATION_COMMANDS = frozenset({"devdash", "devdash-gui"})
APPLICATION_MODULES = frozenset({"devdash", "devdash.gui"})


class SelfManagementError(ValueError):
    pass


def application_unit(name):
    return (
        name.endswith(".service")
        and name.removesuffix(".service").split("@", 1)[0] in APPLICATION_UNITS
    )


def application_command(executable, arguments):
    """Recognize direct launchers; never parse arbitrary shell/script contents."""
    if PurePath(executable).name in APPLICATION_COMMANDS:
        return True
    if not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", PurePath(executable).name):
        return False
    args = iter(arguments[1:])
    for argument in args:
        if argument == "-m":
            return next(args, "") in APPLICATION_MODULES
        if argument.startswith("-m"):
            return argument[2:] in APPLICATION_MODULES
        if argument.startswith("-c"):
            return False
        if argument in ("-W", "-X", "--check-hash-based-pycs"):
            next(args, None)
        elif not argument.startswith("-"):
            script = PurePath(argument)
            return script.name in APPLICATION_COMMANDS or (
                script.parent.name == "devdash" and script.name in ("gui.py", "__main__.py")
            )
    return False


def reject_application_unit(name):
    if application_unit(name):
        raise SelfManagementError(
            f"devdash cannot manage itself: {name}; use systemctl outside devdash"
        )


def reject_application_service(path, unit, service, own_path=None):
    names = [unit.get("Id", ""), *unit.get("Names", [])]
    if (
        path == own_path
        or any(application_unit(name) for name in names)
        or any(application_command(row[0], row[1]) for row in service.get("ExecStart", []))
    ):
        raise SelfManagementError(
            f"devdash cannot manage itself or its containing unit: {unit.get('Id', path)}; use systemctl outside devdash"
        )
