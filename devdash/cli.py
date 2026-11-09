from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from . import __version__
from .generator import generate, preview, save_source
from .installation import template
from .ipc import default_socket, request, serve
from .migration import migrate
from .model import Config


def parser():
    p = argparse.ArgumentParser(description="devdash Observer / Operator / Plans (Core IPC 3)")
    p.add_argument("--version", action="version", version=f"devdash {__version__}")
    p.add_argument("--socket", type=Path, default=default_socket())
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("template").add_argument("kind", choices=("core", "autostart"))
    commands.add_parser("daemon").add_argument("--config", type=Path, required=True)
    commands.add_parser("validate").add_argument("config", type=Path)
    for name in ("status", "cancel", "reload"):
        commands.add_parser(name)
    for name in ("stage", "workload"):
        op = commands.add_parser(name)
        op.add_argument("action", choices=("start", "stop", "restart"))
        op.add_argument("target")
    plan = commands.add_parser("plan")
    plans = plan.add_subparsers(dest="plan_command", required=True)
    preview = plans.add_parser("preview")
    preview.add_argument("kind", choices=("startup", "restart_from", "stop_from"), nargs="?")
    preview.add_argument("--from-stage")
    preview.add_argument("--template")
    execute = plans.add_parser("execute")
    execute.add_argument("preview_id")
    proceed = plans.add_parser("continue")
    proceed.add_argument("run_id")
    proceed.add_argument("token")
    plans.add_parser("cancel").add_argument("run_id")
    plans.add_parser("templates")
    commands.add_parser("next").add_argument("after", help="explicit Stage ID; no saved cursor")
    commands.add_parser("check").add_argument("target")
    commands.add_parser("scan").add_argument("scope", choices=("user", "system"))
    commands.add_parser("logs").add_argument("target")
    child = commands.add_parser("child")
    for key in ("parent", "child", "epoch"):
        child.add_argument(key)
    child.add_argument("action", choices=("start", "stop", "restart"))
    gen = commands.add_parser("generate")
    gen.add_argument("source", type=Path)
    gen.add_argument("output", type=Path)
    gen.add_argument(
        "--write", action="store_true", help="publish verified bundle; never applies units"
    )
    gen.add_argument(
        "--inspect", action="store_true", help="also inspect live manager metadata through Core"
    )
    edit = commands.add_parser("edit")
    edit.add_argument("source", type=Path, nargs="?")
    imp = commands.add_parser("source-import")
    imp.add_argument("source", type=Path)
    imp.add_argument("scope", choices=("user", "system"))
    imp.add_argument("units", nargs="+")
    migration = commands.add_parser("migrate")
    migration.add_argument("source", type=Path)
    migration.add_argument("--output", type=Path)
    migration.add_argument("--ack-readiness-delegation", action="store_true")
    return p


def main():
    args = vars(parser().parse_args())
    socket = args.pop("socket")
    command = args["command"]
    try:
        if command == "template":
            print(template(args["kind"]), end="")
            return
        if command == "daemon":
            asyncio.run(serve(args["config"], socket))
            return
        if command == "validate":
            config = Config.load(args["config"])
            print(f"Valid: {len(config.workloads)} Workloads, {len(config.stages)} Stages")
            return
        if command == "generate":
            source_text = args["source"].read_text()
            config = Config.parse(source_text)
            inspection = None
            if args["inspect"]:
                inspection = asyncio.run(
                    request(socket, dict(command="inspect_config", config=config.json()))
                )
                if any(d["level"] == "error" for r in inspection for d in r["diagnostics"]):
                    raise ValueError("Live dependency conflicts: " + json.dumps(inspection))
            if args["source"].read_text() != source_text:
                raise ValueError("Source changed during generation; retry")
            result = (
                generate(config, args["output"])
                if args["write"]
                else preview(config, args["output"])
            )
            result["live_inspection"] = inspection
        elif command == "edit":
            from .editor import Editor
            from .gui import Window

            window = Window(socket)
            Editor(window, args["source"])
            window.mainloop()
            return
        elif command == "source-import":
            old = args["source"].read_text()
            config = Config.parse(old)
            available = asyncio.run(request(socket, dict(command="scan", scope=args["scope"])))
            for unit in args["units"]:
                if unit not in available:
                    raise ValueError("Unit unavailable in Scan: " + unit)
                asyncio.run(
                    request(socket, dict(command="resolve", scope=args["scope"], unit=unit))
                )
                config.import_unit(unit, args["scope"])
            save_source(args["source"], config, old)
            result = {
                "saved": str(args["source"]),
                "note": "Source only; assign Stages, generate, review/apply drop-ins, explicitly reload Core",
            }
        elif command == "migrate":
            result = migrate(args["source"].read_text())
            if args["output"]:
                if result["readiness_actions"] and not args["ack_readiness_delegation"]:
                    raise ValueError(
                        "Readiness delegation requires explicit acknowledgement; proposal: "
                        + json.dumps(result)
                    )
                output = args["output"]
                if output.exists() or output.is_symlink():
                    raise ValueError("Migration output already exists")
                report = output.with_suffix(output.suffix + ".migration.json")
                report.parent.mkdir(parents=True, exist_ok=True)
                with report.open("x") as stream:
                    stream.write(json.dumps(result, indent=2, ensure_ascii=False))
                save_source(output, Config(result["config"]))
        else:
            if command == "plan":
                subcommand = args.pop("plan_command")
                args["command"] = {
                    "preview": "plan_preview",
                    "execute": "plan_execute",
                    "continue": "plan_continue",
                    "cancel": "plan_cancel",
                    "templates": "plan_templates",
                }[subcommand]
                if subcommand == "preview":
                    args = {key: value for key, value in args.items() if value is not None}
            elif command in ("stage", "workload"):
                args.update(command="operate", kind=command)
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
