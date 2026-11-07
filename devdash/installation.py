"""Print opt-in integration templates; never install or enable anything."""

from importlib.resources import files
from pathlib import Path


def template(kind, home=None):
    name = "devdash-core.service" if kind == "core" else "devdash.desktop.in"
    text = files("devdash").joinpath("templates", name).read_text()
    if kind == "core":
        return text
    if kind != "autostart":
        raise ValueError("Unknown template")
    python = str(
        (Path.home() if home is None else Path(home)) / ".local/share/devdash/current/bin/python"
    )
    if any(character in python for character in ("\n", "\r", "=")):
        raise ValueError("Home path cannot be represented in a desktop Exec field")
    # Desktop string escaping is applied before Exec argument unquoting.
    quoted = '"' + "".join("\\" + c if c in '\\"`$' else c for c in python).replace("%", "%%") + '"'
    command = quoted.replace("\\", "\\\\") + " -I -m devdash.gui"
    return text.replace("@EXEC@", command).replace("@PYTHON@", python.replace("\\", "\\\\"))
