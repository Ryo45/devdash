"""Qt Widgets client. All IPC runs outside the GUI thread."""

from __future__ import annotations

import argparse
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .ipc import default_socket, request


class Window(QMainWindow):
    def __init__(self, socket):
        super().__init__()
        self.socket = socket
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="devdash-ipc")
        self.pending = []
        self.snapshot = None
        self.selection = None
        self.polling = False
        self.setWindowTitle("devdash")
        self.resize(1120, 760)
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        bar = QHBoxLayout()
        self.status = QLabel("Connecting to Core…")
        bar.addWidget(self.status, 1)
        self.scan_button = QPushButton("Scan / Import")
        self.scan_button.setObjectName("scan")
        self.scan_button.clicked.connect(self.scan_dialog)
        bar.addWidget(self.scan_button)
        layout.addLayout(bar)
        splitter = QSplitter()
        layout.addWidget(splitter, 1)
        self.tree = QTreeWidget()
        self.tree.setObjectName("tree")
        self.tree.setHeaderLabels(["Group / Workload", "State"])
        self.tree.setMinimumWidth(350)
        self.tree.setColumnWidth(0, 240)
        self.tree.itemSelectionChanged.connect(self.selected)
        splitter.addWidget(self.tree)
        right = QWidget()
        column = QVBoxLayout(right)
        self.title = QLabel("Select a Group or Workload")
        column.addWidget(self.title)
        operations = QHBoxLayout()
        self.buttons = {}
        for action in ("start", "stop", "restart", "reinitialize"):
            button = QPushButton(action.capitalize())
            button.setObjectName(action)
            button.clicked.connect(lambda checked=False, a=action: self.operate(a))
            operations.addWidget(button)
            self.buttons[action] = button
        self.approve = QPushButton("Approve Gate")
        self.approve.setObjectName("approve")
        self.approve.clicked.connect(self.approve_gate)
        operations.addWidget(self.approve)
        self.cancel = QPushButton("Cancel")
        self.cancel.setObjectName("cancel")
        self.cancel.clicked.connect(lambda: self.send({"command": "cancel"}))
        operations.addWidget(self.cancel)
        column.addLayout(operations)
        self.run = QLabel()
        self.run.setObjectName("run")
        self.run.setWordWrap(True)
        column.addWidget(self.run)
        tabs = QTabWidget()
        self.texts = {}
        for key in ("Details", "Progress", "Diagnostics", "Events", "Logs"):
            text = QPlainTextEdit()
            text.setReadOnly(True)
            text.setObjectName(key.lower())
            tabs.addTab(text, key)
            self.texts[key] = text
        column.addWidget(tabs, 1)
        logs = QPushButton("Refresh selected service logs")
        logs.clicked.connect(self.logs)
        column.addWidget(logs)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 2)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(80)
        self.next_poll = 0
        self.tick()

    def send(self, command, callback=None):
        future = self.pool.submit(asyncio.run, request(self.socket, command))
        self.pending.append((future, callback))

    def tick(self):
        pending, self.pending = self.pending, []
        for future, callback in pending:
            if not future.done():
                self.pending.append((future, callback))
                continue
            try:
                result = future.result()
                if callback:
                    callback(result)
            except Exception as error:
                self.status.setText(str(error))
                self.polling = False
        self.next_poll -= 80
        if self.next_poll <= 0 and not self.polling:
            self.polling = True
            self.next_poll = 700
            self.send({"command": "snapshot"}, self.render)

    def render(self, value):
        self.polling = False
        first_render = self.snapshot is None
        self.snapshot = value
        self.status.setText("Connected · systemd / Docker · Python Core")
        expanded = set()

        def walk(item):
            if item.isExpanded():
                expanded.add(tuple(item.data(0, Qt.UserRole) or ()))
            for index in range(item.childCount()):
                walk(item.child(index))

        for index in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(index))
        selection = self.selection
        self.tree.blockSignals(True)
        self.tree.clear()
        config = value["config"]
        referenced = set()
        selected_item = None

        def node(parent, label, identity, state=""):
            nonlocal selected_item
            item = QTreeWidgetItem([label, state])
            item.setData(0, Qt.UserRole, identity)
            if parent:
                parent.addChild(item)
            else:
                self.tree.addTopLevelItem(item)
            item.setExpanded(first_render or tuple(identity) in expanded)
            if identity == selection:
                selected_item = item
            return item

        def workload(parent, key):
            referenced.add(key)
            work = config["workloads"][key]
            obs = value["observations"].get(key, {})
            scope = work["scope"] if work["backend"] == "systemd" else "docker"
            item = node(parent, f"{key} [{scope}]", ["workload", key], obs.get("state", "unknown"))
            child_items = {}
            for child in obs.get("children", []):
                child_items[child["id"]] = node(
                    item,
                    child.get("name", child["id"]),
                    ["child", key, child["id"], child["epoch"]],
                    child["state"],
                )
            # Only component → service relationships supplied by the controller.
            # Never infer OS process ancestry or create a child dependency graph.
            for child in obs.get("children", []):
                parent = child_items.get(child.get("parent"))
                current = child_items[child["id"]]
                if parent and child["kind"] == "component" and parent is not current:
                    item.takeChild(item.indexOfChild(current))
                    parent.addChild(current)

        def group(parent, key):
            item = node(parent, key, ["group", key])
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
                    node(
                        item,
                        f"Gate: {ref}",
                        ["gate", ref],
                        "approved" if ref in value["approved"] else "waiting",
                    )

        children = {
            step["group"]
            for group_config in config["groups"].values()
            for step in group_config["steps"]
            if "group" in step
        }
        for key in config["groups"].keys() - children:
            group(None, key)
        for key in config["workloads"].keys() - referenced:
            workload(None, key)
        if selected_item:
            self.tree.setCurrentItem(selected_item)
        else:
            self.selection = None
        self.tree.blockSignals(False)
        run = value["run"]
        self.run.setText(
            f"{run['target']} {run['status']} · Gate: {run['waiting_gate'] or '—'}\n{run.get('error') or ''}"
        )
        self.texts["Progress"].setPlainText(json.dumps(run["progress"], indent=2))
        self.texts["Events"].setPlainText(
            "\n".join(
                f"[{event['level']}] {event.get('workload') or ''}: {event['message']}"
                for event in value["events"]
            )
        )
        self.texts["Diagnostics"].setPlainText(
            json.dumps(
                {
                    "inspections": value["inspections"],
                    "monitoring": value["monitoring"],
                    "gap": value["monitor_gap"],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        self.selected()

    def selected(self):
        items = self.tree.selectedItems()
        self.selection = items[0].data(0, Qt.UserRole) if items else None
        value = self.snapshot
        busy = bool(value and value["run"]["status"] in ("running", "cancelling"))
        kind, key, *extra = self.selection or ["", ""]
        self.title.setText(" / ".join(self.selection or ["Select a Group or Workload"]))
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
        self.texts["Details"].setPlainText(json.dumps(details, indent=2, ensure_ascii=False))
        for action, button in self.buttons.items():
            button.setEnabled(not busy and action in actions)
        gate = value["run"]["waiting_gate"] if value else None
        manual_wait = bool(gate and value["config"]["gates"][gate]["type"] == "manual")
        self.approve.setEnabled(
            bool(
                value
                and (
                    manual_wait
                    or kind == "gate"
                    and value["config"]["gates"][key]["type"] == "manual"
                )
            )
        )
        self.cancel.setEnabled(busy and value["run"]["status"] != "cancelling")

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
                {"command": "logs", "target": self.selection[1]}, self.texts["Logs"].setPlainText
            )

    def scan_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Scan / Import existing systemd services")
        dialog.resize(650, 500)
        layout = QVBoxLayout(dialog)
        scope = QComboBox()
        scope.addItems(["user", "system"])
        layout.addWidget(scope)
        units = QListWidget()
        layout.addWidget(units)
        message = QLabel("Scan is on demand. Import adds references only.")
        layout.addWidget(message)

        def populate(result):
            units.clear()
            for unit in result:
                name = unit if isinstance(unit, str) else unit.get("unit", unit.get("name", ""))
                item = QListWidgetItem(name)
                item.setCheckState(Qt.Unchecked)
                units.addItem(item)

        def scan():
            selected_scope = scope.currentText()

            def received(result):
                if scope.currentText() == selected_scope:
                    populate(result)

            self.send({"command": "scan", "scope": selected_scope}, received)

        scope.currentTextChanged.connect(lambda _: units.clear())

        button = QPushButton("Scan")
        button.clicked.connect(scan)
        layout.addWidget(button)

        def imported(result):
            message.setText("Imported: " + ", ".join(result))

        def do_import():
            selected = [
                units.item(i).text()
                for i in range(units.count())
                if units.item(i).checkState() == Qt.Checked
            ]
            if selected:
                self.send(
                    {"command": "import", "scope": scope.currentText(), "units": selected}, imported
                )

        button = QPushButton("Import checked services")
        button.clicked.connect(do_import)
        layout.addWidget(button)
        # Non-modal: the main event loop continues collecting IPC replies.
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.show()

    def closeEvent(self, event):
        self.timer.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)
        super().closeEvent(event)


def apply_theme(app):
    app.setStyle("Fusion")
    app.setStyleSheet(
        "QWidget { background:#22262d; color:#e1e5ec; } QPushButton { padding:7px; } QPushButton:disabled { color:#747a84; } QPlainTextEdit,QTreeWidget,QListWidget { background:#191d23; }"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", default=default_socket())
    args = parser.parse_args()
    app = QApplication([])
    apply_theme(app)
    window = Window(args.socket)
    window.show()
    raise SystemExit(app.exec())
