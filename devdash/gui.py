"""ttkbootstrap/Tkinter client. Tk stays on the main thread; IPC uses workers."""

from __future__ import annotations

import argparse
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from tkinter.scrolledtext import ScrolledText

import ttkbootstrap as ttk

from .ipc import default_socket, request


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
        self.status = ttk.Label(bar, text="Connecting to Core…")
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
            left, columns=("state",), selectmode="browse", show="tree headings"
        )
        self.tree.heading("#0", text="Group / Workload")
        self.tree.heading("state", text="State")
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
            right, text="Select a Group or Workload", font=("", 12, "bold")
        )
        self.title_label.pack(fill="x", pady=(0, 10))
        operations = ttk.Frame(right)
        operations.pack(fill="x")
        self.buttons = {}
        for action in ("start", "stop", "restart", "reinitialize"):
            button = ttk.Button(
                operations, text=action.capitalize(), command=lambda a=action: self.operate(a)
            )
            button.pack(side="left", padx=(0, 5))
            self.buttons[action] = button
        self.approve = ttk.Button(
            operations, text="Approve Gate", bootstyle="success", command=self.approve_gate
        )
        self.approve.pack(side="left", padx=(0, 5))
        self.cancel = ttk.Button(
            operations,
            text="Cancel",
            bootstyle="warning",
            command=lambda: self.send({"command": "cancel"}, self.render),
        )
        self.cancel.pack(side="left")
        self.run = ttk.Label(right, text="", wraplength=730)
        self.run.pack(fill="x", pady=10)
        self.tabs = ttk.Notebook(right)
        self.tabs.pack(fill="both", expand=True)
        self.texts = {}
        for key in ("Details", "Progress", "Diagnostics", "Events", "Logs"):
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
        if command["command"] != "snapshot":
            self.error.configure(text="")
        future = self.pool.submit(asyncio.run, request(self.socket, command))
        self.pending.append((future, callback, command["command"] == "snapshot"))

    def tick(self):
        if self.closed:
            return
        pending, self.pending = self.pending, []
        for future, callback, polling in pending:
            if not future.done():
                self.pending.append((future, callback, polling))
                continue
            if polling:
                self.polling = False
            try:
                result = future.result()
                if callback:
                    callback(result)
            except Exception as error:
                if polling:
                    self.status.configure(text=f"Core unavailable · retrying: {error}")
                else:
                    self.error.configure(text=str(error))
        self.next_poll -= 80
        if self.next_poll <= 0 and not self.polling:
            self.polling = True
            self.next_poll = 700
            self.send({"command": "snapshot"}, self.render)
        self.timer = self.after(80, self.tick)

    def render(self, value):
        first_render = self.snapshot is None
        self.snapshot = value
        self.status.configure(text="Connected · systemd / Docker · Python Core")
        config = value["config"]
        identities = {}
        referenced = set()

        def node(parent, label, identity, state=""):
            iid = json.dumps(identity, ensure_ascii=False)
            identities[iid] = identity
            if self.tree.exists(iid):
                self.tree.item(iid, text=label, values=(state,))
                self.tree.move(iid, parent, "end")
            else:
                self.tree.insert(
                    parent, "end", iid=iid, text=label, values=(state,), open=first_render
                )
            return iid

        def workload(parent, key):
            referenced.add(key)
            work = config["workloads"][key]
            obs = value["observations"].get(key, {})
            scope = work["scope"] if work["backend"] == "systemd" else "docker"
            label = work.get("name") or key
            item = node(
                parent, f"{label} [{scope}]", ["workload", key], obs.get("state", "unknown")
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

        def group(parent, key):
            item = node(parent, config["groups"][key].get("name") or key, ["group", key])
            for step in config["groups"][key]["steps"]:
                kind, ref = next(iter(step.items()))
                if kind == "group":
                    group(item, ref)
                elif kind == "workload":
                    workload(item, ref)
                elif kind == "parallel":
                    for work in ref:
                        workload(item, work)
                else:
                    gate = config["gates"][ref]
                    state = (
                        "approved"
                        if ref in value["approved"]
                        else "unapproved"
                        if gate["type"] == "manual"
                        else "check on start"
                    )
                    node(item, f"Gate: {ref}", ["gate", ref], state)

        children = {
            step["group"]
            for g in config["groups"].values()
            for step in g["steps"]
            if "group" in step
        }
        for key in config["groups"]:
            if key not in children:
                group("", key)
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
            text=f"{run['target']} {run['status']} · Gate: {run['waiting_gate'] or '—'}\n{run.get('error') or ''}"
        )
        set_text(self.texts["Progress"], json.dumps(run["progress"], indent=2))
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
        self.selected()

    def selected(self, event=None):
        items = self.tree.selection()
        self.selection = self.identities.get(items[0]) if items else None
        value = self.snapshot
        busy = bool(value and value["run"]["status"] in ("running", "cancelling"))
        kind, key, *extra = self.selection or ["", ""]
        self.title_label.configure(
            text=" / ".join((self.selection or ["Select a Group or Workload"])[:3])
        )
        details = {}
        actions = []
        if value and kind in ("group", "workload"):
            details = value["config"]["groups" if kind == "group" else "workloads"][key]
            details = dict(details, observation=value["observations"].get(key))
            actions = ["start", "stop", "restart"] + (["reinitialize"] if kind == "group" else [])
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
        elif value and kind == "gate":
            details = value["config"]["gates"][key]
        set_text(self.texts["Details"], json.dumps(details, indent=2, ensure_ascii=False))
        for action, button in self.buttons.items():
            button.configure(state="normal" if not busy and action in actions else "disabled")
        gate = value["run"]["waiting_gate"] if value else None
        manual_wait = bool(gate and value["config"]["gates"][gate]["type"] == "manual")
        manual_selected = bool(
            value and kind == "gate" and value["config"]["gates"][key]["type"] == "manual"
        )
        self.approve.configure(state="normal" if manual_wait or manual_selected else "disabled")
        self.cancel.configure(
            state="normal" if busy and value["run"]["status"] != "cancelling" else "disabled"
        )

    def operate(self, action):
        if not self.selection:
            return
        kind, key, *extra = self.selection
        command = dict(command="operate", target=key, group=kind == "group", action=action)
        if kind == "child":
            command = dict(
                command="child", parent=key, child=extra[0], epoch=extra[1], action=action
            )
        self.send(command, self.render)

    def approve_gate(self):
        gate = self.snapshot["run"]["waiting_gate"]
        if gate and self.snapshot["config"]["gates"][gate]["type"] != "manual":
            gate = None
        if not gate and self.selection and self.selection[0] == "gate":
            gate = self.selection[1]
        if gate:
            self.send({"command": "approve", "gate": gate}, self.render)

    def logs(self):
        if self.selection and self.selection[0] in ("workload", "child"):
            self.send(
                {"command": "logs", "target": self.selection[1]},
                lambda result: set_text(self.texts["Logs"], result),
            )

    def scan_dialog(self):
        return ScanDialog(self)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.timer:
            self.after_cancel(self.timer)
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.destroy()


class ScanDialog(ttk.Toplevel):
    """Non-modal scan. Selection always belongs to the displayed manager."""

    def __init__(self, owner):
        super().__init__(
            title="Scan / Import existing systemd services", size=(650, 500), transient=owner
        )
        self.owner = owner
        self.closed = False
        self.generation = 0
        self.protocol("WM_DELETE_WINDOW", self.close)
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        self.scope = ttk.Combobox(frame, values=("user", "system"), state="readonly")
        self.scope.set("user")
        self.scope.pack(fill="x")
        self.scope.bind("<<ComboboxSelected>>", self.reset)
        self.units = ttk.Treeview(frame, columns=("unit",), show="headings", selectmode="extended")
        self.units.heading(
            "unit", text="Existing services — select to import (Ctrl/Shift for multiple)"
        )
        self.units.column("unit", width=570)
        self.units.pack(fill="both", expand=True, pady=10)
        self.message = ttk.Label(
            frame, text="Scan is on demand. Import adds references only.", wraplength=610
        )
        self.message.pack(fill="x")
        self.scan_button = ttk.Button(frame, text="Scan", command=self.scan)
        self.scan_button.pack(fill="x", pady=5)
        self.import_button = ttk.Button(
            frame, text="Import selected services", command=self.do_import
        )
        self.import_button.pack(fill="x")

    def reset(self, event=None):
        self.generation += 1
        self.units.delete(*self.units.get_children())

    def scan(self):
        self.reset()
        generation, scope = self.generation, self.scope.get()

        def received(result):
            if not self.closed and self.generation == generation and self.scope.get() == scope:
                for unit in result:
                    self.units.insert("", "end", iid=unit, values=(unit,))

        self.owner.send({"command": "scan", "scope": scope}, received)

    def do_import(self):
        units = list(self.units.selection())
        if units:

            def imported(result):
                if not self.closed:
                    self.message.configure(text="Imported: " + ", ".join(result))

            self.owner.send(
                {"command": "import", "scope": self.scope.get(), "units": units}, imported
            )

    def close(self):
        self.closed = True
        self.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", default=default_socket())
    args = parser.parse_args()
    window = Window(args.socket)
    window.mainloop()
