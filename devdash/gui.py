"""ttkbootstrap/Tkinter client. Tk stays on the main thread; IPC uses workers."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from tkinter.scrolledtext import ScrolledText

import ttkbootstrap as ttk

from . import __version__
from .ipc import OutcomeUnknown, default_socket, request
from .presentation import plan_progress, stage_progress, workload_summary
from .protocol import MUTATIONS, CompatibilityError, check_core


def set_text(widget, text):
    """Keep logs/diagnostics selectable without resetting the scroll on every poll."""
    if widget.get("1.0", "end-1c") == text:
        return
    position = widget.yview()
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", text)
    widget.configure(state="disabled")
    if position:
        widget.yview_moveto(position[0])


class Window(ttk.Window):
    def __init__(self, socket):
        super().__init__(
            title="devdash", theme="bootstrap-dark", size=(1160, 780), minsize=(980, 600)
        )
        self.socket = socket
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="devdash-ipc")
        self.pending = []
        self.snapshot = None
        self.connected = False
        self.selection = None
        self.identities = {}
        self.polling = False
        self.closed = False
        self.timer = None
        self.next_poll = 0
        self.protocol("WM_DELETE_WINDOW", self.close)
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)
        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=(0, 10))
        self.status = ttk.Label(bar, text="Connecting to Core…", wraplength=930)
        self.status.pack(side="left", fill="x", expand=True)
        self.scan_button = ttk.Button(bar, text="Scan / Import", command=self.scan_dialog)
        self.scan_button.pack(side="right")
        self.error = ttk.Label(outer, text="", bootstyle="danger", wraplength=1100)
        self.error.pack(fill="x")
        split = ttk.Panedwindow(outer, orient="horizontal")
        split.pack(fill="both", expand=True)
        left = ttk.Frame(split)
        split.add(left, weight=1)
        self.tree = ttk.Treeview(
            left, columns=("state", "readiness"), selectmode="browse", show="tree headings"
        )
        self.tree.heading("#0", text="Stage / Workload")
        self.tree.heading("state", text="Execution")
        self.tree.heading("readiness", text="Readiness")
        self.tree.column("readiness", width=95, minwidth=75, stretch=False)
        self.tree.column("#0", width=250, minwidth=170)
        self.tree.column("state", width=100, minwidth=80, stretch=False)
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.selected)
        right = ttk.Frame(split, padding=(12, 0, 0, 0))
        split.add(right, weight=3)
        self.title_label = ttk.Label(
            right, text="Select a Stage or Workload", font=("", 12, "bold")
        )
        self.title_label.pack(fill="x", pady=(0, 10))
        operations = ttk.Frame(right)
        operations.pack(fill="x")
        self.buttons = {}
        for action in ("start", "stop", "restart"):
            button = ttk.Button(
                operations, text=action.capitalize(), command=lambda a=action: self.operate(a)
            )
            button.pack(side="left", padx=(0, 5))
            self.buttons[action] = button
        self.next_button = ttk.Button(operations, text="Next Stage", command=self.next_stage)
        self.next_button.pack(side="left", padx=(0, 5))
        self.cancel = ttk.Button(
            operations,
            text="Cancel unissued",
            bootstyle="warning",
            command=lambda: self.send({"command": "cancel"}, self.render),
        )
        self.cancel.pack(side="left")
        settings = ttk.Frame(right)
        settings.pack(fill="x", pady=(6, 0))
        ttk.Button(settings, text="Configuration Editor", command=self.open_editor).pack(
            side="left"
        )
        ttk.Button(
            settings,
            text="Reload Core config (no lifecycle)",
            command=lambda: self.send({"command": "reload"}, self.render),
        ).pack(side="left", padx=5)
        plan_controls = ttk.Frame(right)
        plan_controls.pack(fill="x", pady=(6, 0))
        ttk.Label(plan_controls, text="On-demand Plan: ").pack(side="left")
        self.plan_kind = ttk.Combobox(
            plan_controls,
            values=("startup", "restart_from", "stop_from"),
            state="readonly",
            width=18,
        )
        self.plan_kind.set("startup")
        self.plan_kind.pack(side="left")
        self.plan_preview_button = ttk.Button(
            plan_controls, text="Preview Plan", command=self.preview_plan
        )
        self.plan_preview_button.pack(side="left", padx=4)
        self.plan_continue = ttk.Button(
            plan_controls, text="Continue Checkpoint", command=self.continue_plan
        )
        self.plan_continue.pack(side="left", padx=4)
        self.plan_abort = ttk.Button(
            plan_controls, text="Abort Plan (no rollback)", command=self.abort_plan
        )
        self.plan_abort.pack(side="left")
        self.summary = ttk.Label(right, text="", wraplength=730, justify="left")
        self.summary.pack(fill="x", pady=(10, 0))
        self.run = ttk.Label(right, text="", wraplength=730)
        self.run.pack(fill="x", pady=10)
        self.tabs = ttk.Notebook(right)
        self.tabs.pack(fill="both", expand=True)
        self.texts = {}
        for key in ("Details", "Progress", "Plan", "Diagnostics", "Events", "Logs"):
            frame = ttk.Frame(self.tabs)
            text = ScrolledText(
                frame,
                wrap="word",
                width=50,
                height=15,
                background="#191d23",
                foreground="#e1e5ec",
                insertbackground="#e1e5ec",
                relief="flat",
                font="TkFixedFont",
                state="disabled",
            )
            text.pack(fill="both", expand=True)
            self.tabs.add(frame, text=key)
            self.texts[key] = text
        ttk.Button(right, text="Refresh selected service logs", command=self.logs).pack(
            fill="x", pady=(8, 0)
        )
        self.selected()
        self.timer = self.after(0, self.tick)

    def send(self, command, callback=None):
        if self.closed:
            return
        command = dict(command)
        instance = self.snapshot["core"]["instance"] if self.snapshot else None
        if command["command"] in MUTATIONS:
            if not self.connected:
                self.error.configure(text="Core is not connected; wait for fresh observations")
                return
            command["core_instance"] = instance
            command["config_revision"] = self.snapshot["config_revision"]
        if command["command"] != "snapshot":
            self.error.configure(text="")
        future = self.pool.submit(asyncio.run, request(self.socket, command))
        self.pending.append((future, callback, command["command"], instance))

    def disconnected(self, error):
        self.connected = False
        self.status.configure(
            text=f"Core disconnected · showing last observations (stale) · retrying: {error}"
        )
        for iid in self.identities:
            values = self.tree.item(iid, "values")
            if values and values[0] and not values[0].endswith(" (stale)"):
                self.tree.item(iid, values=(values[0] + " (stale)", "unknown (stale)"))
        if self.snapshot:
            set_text(
                self.texts["Plan"],
                "STALE: Core disconnected; Plan outcome unknown\n" + plan_progress(self.snapshot),
            )
        self.selected()

    def tick(self):
        if self.closed:
            return
        pending, self.pending = self.pending, []
        for future, callback, command, instance in pending:
            polling = command == "snapshot"
            if not future.done():
                self.pending.append((future, callback, command, instance))
                continue
            if polling:
                self.polling = False
            try:
                result = future.result()
                if (
                    not polling
                    and instance
                    and self.snapshot
                    and instance != self.snapshot["core"]["instance"]
                ):
                    if command in MUTATIONS:
                        self.error.configure(
                            text="Core restarted; previous operation outcome unknown; old response discarded"
                        )
                    continue
                if (
                    isinstance(result, dict)
                    and "config_revision" in result
                    and self.snapshot
                    and result.get("core", {}).get("instance") == self.snapshot["core"]["instance"]
                    and result["config_revision"] < self.snapshot["config_revision"]
                ):
                    continue  # A delayed acknowledgement must not restore an old configuration.
                if callback:
                    callback(result)
            except Exception as error:
                if polling:
                    self.disconnected(error)
                else:
                    self.error.configure(text=str(error))
                    if isinstance(error, (OutcomeUnknown, CompatibilityError)):
                        self.disconnected(error)
                        self.next_poll = 0
        self.next_poll -= 80
        if self.next_poll <= 0 and not self.polling:
            self.polling = True
            self.next_poll = 700
            self.send({"command": "snapshot"}, self.render)
        self.timer = self.after(80, self.tick)

    def render(self, value):
        check_core(value.get("core"))
        first_render = self.snapshot is None
        previous = self.snapshot
        if previous and previous["core"]["instance"] != value["core"]["instance"]:
            message = "New Core instance; observations reset; no operation was replayed"
            if previous["run"]["status"] in ("running", "cancelling"):
                message += "; previous operation outcome unknown"
            self.error.configure(text=message)
        self.snapshot = value
        self.connected = True
        version = value["core"]["version"]
        mismatch = " · version mismatch (IPC compatible)" if version != __version__ else ""
        self.status.configure(
            text=f"Connected · Core {version} · GUI {__version__} · IPC {value['core']['protocol']}{mismatch}"
        )
        config = value["config"]
        identities = {}
        referenced = set()

        def node(parent, label, identity, state="", readiness=""):
            iid = json.dumps(identity, ensure_ascii=False)
            identities[iid] = identity
            if self.tree.exists(iid):
                self.tree.item(iid, text=label, values=(state, readiness))
                self.tree.move(iid, parent, "end")
            else:
                self.tree.insert(
                    parent, "end", iid=iid, text=label, values=(state, readiness), open=first_render
                )
            return iid

        def workload(parent, key):
            referenced.add(key)
            work = config["workloads"][key]
            obs = value["observations"].get(key, {})
            scope = work["scope"] if work["backend"] == "systemd" else "docker"
            label = work.get("name") or key
            item = node(
                parent,
                f"{label} [{scope}]",
                ["workload", key],
                obs.get("state", "unknown") + (" (stale)" if obs.get("stale") else ""),
                "unknown" if obs.get("stale") else obs.get("readiness", {}).get("state", "unknown"),
            )
            child_items = {}
            for child in obs.get("children", []):
                child_items[child["id"]] = node(
                    item,
                    child.get("name", child["id"]),
                    ["child", key, child["id"], child["epoch"]],
                    child["state"],
                )
            # Reparent components only to the service supplied by the controller.
            children = {child["id"]: child for child in obs.get("children", [])}
            for child in children.values():
                parent_id = child.get("parent")
                if (
                    child["kind"] == "component"
                    and children.get(parent_id, {}).get("kind") == "service"
                ):
                    self.tree.move(child_items[child["id"]], child_items[parent_id], "end")

        for stage in config["stages"]:
            item = node("", stage["name"] or stage["id"], ["stage", stage["id"]])
            for key in stage["workloads"]:
                workload(item, key)
        for key in config["workloads"]:
            if key not in referenced:
                workload("", key)
        for iid in self.identities.keys() - identities.keys():
            if self.tree.exists(iid):
                self.tree.delete(iid)
        self.identities = identities
        if self.selection:
            iid = json.dumps(self.selection, ensure_ascii=False)
            if iid in identities:
                self.tree.selection_set(iid)
            else:
                self.tree.selection_remove(*self.tree.selection())
                self.selection = None  # Never fall back to parent after an epoch change.
        run = value["run"]
        self.run.configure(
            text=f"{run['kind']} {run['target']} {run['action']} · {run['status']}\n{run.get('error') or ''}"
            if run["status"]
            else "No operation history in this Core instance; select a Stage explicitly"
        )
        set_text(
            self.texts["Events"],
            "\n".join(
                f"[{event['level']}] {event.get('workload') or ''}: {event['message']}"
                for event in value["events"]
            ),
        )
        set_text(
            self.texts["Diagnostics"],
            json.dumps(
                {
                    "inspections": value["inspections"],
                    "monitoring": value["monitoring"],
                    "gap": value["monitor_gap"],
                },
                indent=2,
                ensure_ascii=False,
            ),
        )
        set_text(self.texts["Plan"], plan_progress(value))
        choices = (
            "startup",
            "restart_from",
            "stop_from",
            *["template:" + key for key in config.get("plan_templates", {})],
        )
        self.plan_kind.configure(values=choices)
        if self.plan_kind.get() not in choices:
            self.plan_kind.set("startup")
        self.selected()

    def selected(self, event=None):
        items = self.tree.selection()
        self.selection = self.identities.get(items[0]) if items else None
        value = self.snapshot
        busy = bool(value and value["run"]["status"] in ("running", "cancelling"))
        kind, key, *extra = self.selection or ["", ""]
        self.title_label.configure(
            text=" / ".join((self.selection or ["Select a Stage or Workload"])[:3])
        )
        details = {}
        actions = []
        if value and kind in ("stage", "workload"):
            details = (
                next(stage for stage in value["config"]["stages"] if stage["id"] == key)
                if kind == "stage"
                else value["config"]["workloads"][key]
            )
            details = dict(
                details,
                observation=value["observations"].get(key),
                observation_current=self.connected,
            )
            actions = ["start", "stop", "restart"]
        elif value and kind == "child":
            details = next(
                (
                    child
                    for child in value["observations"].get(key, {}).get("children", [])
                    if child["id"] == extra[0] and child["epoch"] == extra[1]
                ),
                {},
            )
            actions = details.get("actions", [])
        summary = ""
        progress = "Select a Stage to see requested operations and the explicit Next target."
        if value and kind == "workload":
            summary = workload_summary(
                value["config"]["workloads"][key],
                value["observations"].get(key, {}),
                self.connected,
            )
        elif value and kind == "stage":
            summary = "Stage members start independently. No later Stage starts automatically.\nCancel skips undispatched requests; it does not cancel jobs or stop Workloads."
            progress = stage_progress(value, key)
            if not self.connected:
                progress = "STALE: Core disconnected; operation outcome may be unknown\n" + progress
        elif value and kind == "child":
            summary = "Internal child: operations go to its Child Controller. Parent Lifecycle is separate."
        self.summary.configure(text=summary)
        set_text(self.texts["Progress"], progress)
        set_text(self.texts["Details"], json.dumps(details, indent=2, ensure_ascii=False))
        destination = {"stage": "Stage", "child": "child"}.get(kind, "Lifecycle")
        if value and kind == "workload":
            destination = value["config"]["workloads"][key]["backend"]
        for action, button in self.buttons.items():
            button.configure(
                text=f"{action.capitalize()} ({destination})",
                state="normal" if self.connected and not busy and action in actions else "disabled",
            )
        following = None
        if value and kind == "stage":
            ids = [stage["id"] for stage in value["config"]["stages"]]
            index = ids.index(key) + 1
            if index < len(ids):
                following = value["config"]["stages"][index]
        self.next_button.configure(
            text=f"Next: {following['name']} ({', '.join(following['workloads'])})"
            if following
            else "No Next Stage",
            state="normal" if self.connected and not busy and following else "disabled",
        )
        plan = value.get("plan") if value else None
        self.cancel.configure(
            state="normal"
            if self.connected and busy and not plan and value["run"]["status"] != "cancelling"
            else "disabled"
        )
        checkpoint = plan.get("checkpoint") if plan else None
        self.plan_continue.configure(
            text="Continue (observations unknown)"
            if checkpoint and checkpoint["unknown"]
            else "Continue Checkpoint",
            state="normal"
            if self.connected and checkpoint and value["run"]["status"] != "cancelling"
            else "disabled",
        )
        self.plan_abort.configure(
            state="normal"
            if self.connected and plan and value["run"]["status"] != "cancelling"
            else "disabled"
        )
        self.plan_preview_button.configure(state="normal" if self.connected else "disabled")

        self.scan_button.configure(state="normal" if self.connected else "disabled")

    def operate(self, action):
        if not self.selection:
            return
        kind, key, *extra = self.selection
        command = dict(command="operate", target=key, kind=kind, action=action)
        if kind == "child":
            command = dict(
                command="child", parent=key, child=extra[0], epoch=extra[1], action=action
            )
        self.send(command, self.render)

    def preview_plan(self):
        choice = self.plan_kind.get()
        command = dict(command="plan_preview")
        if choice.startswith("template:"):
            command["template"] = choice.removeprefix("template:")
        else:
            command["kind"] = choice
            if choice != "startup":
                if not self.selection or self.selection[0] != "stage":
                    self.error.configure(text="Select the first Stage of the requested suffix")
                    return
                command["from_stage"] = self.selection[1]
        self.send(command, lambda value: PlanDialog(self, value))

    def continue_plan(self):
        plan = self.snapshot.get("plan")
        if plan and plan.get("checkpoint"):
            self.send(
                dict(command="plan_continue", run_id=plan["id"], token=plan["checkpoint"]["token"]),
                self.render,
            )

    def abort_plan(self):
        plan = self.snapshot.get("plan")
        if plan:
            self.send(dict(command="plan_cancel", run_id=plan["id"]), self.render)

    def next_stage(self):
        if self.selection and self.selection[0] == "stage":
            after = self.selection[1]
            stages = self.snapshot["config"]["stages"]
            index = next(i for i, stage in enumerate(stages) if stage["id"] == after) + 1
            if index >= len(stages):
                return
            target = stages[index]["id"]

            def accepted(value):
                self.render(value)
                iid = json.dumps(["stage", target], ensure_ascii=False)
                if self.tree.exists(iid):
                    self.tree.selection_set(iid)
                    self.selected()

            self.send(dict(command="next", after=after), accepted)

    def open_editor(self):
        from .editor import Editor

        return Editor(self)

    def logs(self):
        if self.selection and self.selection[0] in ("workload", "child"):
            self.send(
                {"command": "logs", "target": self.selection[1]},
                lambda result: set_text(self.texts["Logs"], result),
            )

    def scan_dialog(self):
        editor = self.open_editor()
        editor.scan()
        return editor

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.timer:
            self.after_cancel(self.timer)
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.destroy()


class PlanDialog(ttk.Toplevel):
    """Preview is inert; Execute is a separate explicit request."""

    def __init__(self, owner, preview):
        super().__init__(title="Plan preview — no operations issued", size=(760, 600))
        self.owner, self.preview = owner, preview
        text = ScrolledText(self, wrap="word")
        text.pack(fill="both", expand=True, padx=10, pady=10)
        text.insert("1.0", json.dumps(preview, indent=2, ensure_ascii=False))
        text.configure(state="disabled")
        self.execute_button = ttk.Button(
            self, text="Execute this one-time Plan", command=self.execute
        )
        self.execute_button.pack(fill="x", padx=10, pady=8)
        ttk.Button(self, text="Close preview (no effect)", command=self.destroy).pack(
            fill="x", padx=10, pady=8
        )
        owner.plan_dialog = self

    def execute(self):
        if not self.owner.connected or self.owner.snapshot["run"]["status"] in (
            "running",
            "cancelling",
        ):
            self.owner.error.configure(text="Another operation is running or Core is unavailable")
            return

        def accepted(value):
            self.owner.render(value)
            self.owner.tabs.select(list(self.owner.texts).index("Plan"))
            if self.winfo_exists():
                self.destroy()

        self.owner.send(dict(command="plan_execute", preview_id=self.preview["id"]), accepted)


@contextmanager
def gui_instance(socket, new_window=False):
    """A per-socket Linux lock, separate from Core's RuntimeDirectory."""
    if new_window:
        yield True
        return
    directory = (
        Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/devdash-{os.getuid()}")) / "devdash-gui"
    )
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise ValueError("GUI lock directory must belong to the current user and have mode 0700")
    name = hashlib.sha256(str(Path(socket).resolve()).encode()).hexdigest() + ".lock"
    with (directory / name).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", action="version", version=f"devdash-gui {__version__}")
    parser.add_argument("--socket", default=default_socket())
    parser.add_argument("--new-window", action="store_true", help="Allow another GUI for this Core")
    args = parser.parse_args()
    with gui_instance(args.socket, args.new_window) as acquired:
        if not acquired:
            print("devdash GUI is already running for this Core")
            return
        window = Window(args.socket)
        window.mainloop()


if __name__ == "__main__":
    main()
