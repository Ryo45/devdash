"""Compatibility majors are independent of package release versions."""

from . import __version__

CORE_PROTOCOL = 2
BRIDGE_PROTOCOL = 1
MUTATIONS = frozenset({"operate", "next", "child", "cancel", "reload"})


class CompatibilityError(RuntimeError):
    pass


def check_core(info):
    if (
        not isinstance(info, dict)
        or type(info.get("protocol")) is not int
        or info["protocol"] != CORE_PROTOCOL
    ):
        actual = info.get("protocol", "missing") if isinstance(info, dict) else "missing"
        raise CompatibilityError(
            f"Incompatible Core IPC: expected {CORE_PROTOCOL}, received {actual}; update Core/GUI. No operation was retried."
        )
    if (
        not isinstance(info.get("version"), str)
        or not isinstance(info.get("instance"), str)
        or not info["instance"]
    ):
        raise CompatibilityError("Core identity incomplete; reconnect before operating")


def check_request(value, info):
    if type(value.get("protocol")) is not int or value["protocol"] != info["protocol"]:
        raise CompatibilityError(
            f"Core {info['version']} requires IPC {info['protocol']}; client sent {value.get('protocol', 'missing')}"
        )
    if value.get("command") in MUTATIONS and value.get("core_instance") != info["instance"]:
        raise CompatibilityError(
            "Core instance changed; refresh observations before operating. Request rejected."
        )


def bridge_info(reply):
    info = reply.get("bridge", {})
    if not isinstance(info, dict):
        info = {}
    protocol = info.get("protocol")
    return dict(
        kind="roslaunch",
        version=info.get("version", "unknown"),
        protocol=protocol,
        compatible=type(protocol) is int and protocol == BRIDGE_PROTOCOL,
        epoch=reply.get("epoch"),
    )


def check_bridge(reply):
    info = bridge_info(reply)
    if not info["compatible"]:
        raise CompatibilityError(
            f"Incompatible ROS Bridge IPC: expected {BRIDGE_PROTOCOL}, received {info['protocol']}; Bridge {info['version']}. Child operations disabled."
        )
    return info


def core_identity(instance):
    return dict(version=__version__, protocol=CORE_PROTOCOL, instance=instance)
