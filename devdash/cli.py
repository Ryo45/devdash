from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .ipc import default_socket, default_state, request, serve
from .model import Config


def parser():
    p = argparse.ArgumentParser(description="devdash — Linux workload dashboard")
    p.add_argument("--socket", type=Path, default=default_socket())
    commands = p.add_subparsers(dest="command", required=True)
    daemon = commands.add_parser("daemon")
    daemon.add_argument("--config", type=Path, required=True)
    daemon.add_argument("--state", type=Path, default=default_state())
    validate = commands.add_parser("validate")
    validate.add_argument("config", type=Path)
    for name in ("status", "cancel"):
        commands.add_parser(name)
    for name in ("group", "workload"):
        op = commands.add_parser(name)
        op.add_argument(
            "action",
            choices=("start", "stop", "restart", "reinitialize")
            if name == "group"
            else ("start", "stop", "restart"),
        )
        op.add_argument("target")
    approve = commands.add_parser("approve")
    approve.add_argument("gate")
    check = commands.add_parser("check")
    check.add_argument("target")
    scan = commands.add_parser("scan")
    scan.add_argument("scope", choices=("user", "system"))
    imp = commands.add_parser("import")
    imp.add_argument("scope", choices=("user", "system"))
    imp.add_argument("units", nargs="+")
    logs = commands.add_parser("logs")
    logs.add_argument("target")
    child = commands.add_parser("child")
    for key in ("parent", "child", "epoch"):
        child.add_argument(key)
    child.add_argument("action", choices=("start", "stop", "restart"))
    return p


def main():
    args = vars(parser().parse_args())
    socket = args.pop("socket")
    command = args["command"]
    try:
        if command == "daemon":
            asyncio.run(serve(args["config"], socket, args["state"]))
            return
        if command == "validate":
            config = Config.load(args["config"])
            print(f"Valid: {len(config.workloads)} Workloads, {len(config.groups)} Groups")
            return
        if command in ("group", "workload"):
            args.update(command="operate", group=command == "group")
        elif command == "status":
            args["command"] = "snapshot"
        elif command == "check":
            args["command"] = "inspect"
        result = asyncio.run(request(socket, args))
        print(
            result if isinstance(result, str) else json.dumps(result, indent=2, ensure_ascii=False)
        )
    except (OSError, ValueError, RuntimeError) as error:
        raise SystemExit(str(error)) from None
