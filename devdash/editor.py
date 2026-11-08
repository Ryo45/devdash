"""Local configuration editor. Same Config/Generator as CLI; no host apply operation."""

import copy
import json
from pathlib import Path
from tkinter import filedialog
from tkinter.scrolledtext import ScrolledText

import ttkbootstrap as ttk
import yaml

from .generator import generate, preview, save_source
from .model import Config, fields, parse_yaml


class Editor(ttk.Toplevel):
    def __init__(self, owner, source=None):
        super().__init__(
            title="Stage Configuration Editor — source / preview / generate", size=(1050, 780)
        )
        self.owner = owner
        self.closed = False
        self.expected = None
        self.loaded_path = None
        self.config = Config({})
        self.protocol("WM_DELETE_WINDOW", self.close)
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)
        self.source = ttk.StringVar(value=str(source or ""))
        self.output = ttk.StringVar(value="")
        for label, variable, choices in (
            ("Source YAML", self.source, (("Open", self.open_source), ("Save source", self.save))),
            (
                "Generated output directory",
                self.output,
                (
                    ("Choose", self.choose_output),
                    ("Preview", self.preview),
                    ("Generate", self.generate),
                ),
            ),
        ):
            row = ttk.Frame(outer)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=label, width=25).pack(side="left")
            ttk.Entry(row, textvariable=variable).pack(side="left", fill="x", expand=True)
            for text, command in choices:
                ttk.Button(row, text=text, command=lambda fn=command: self.guard(fn)).pack(
                    side="left", padx=3
                )
        row = ttk.Frame(outer)
        row.pack(fill="x")
        ttk.Button(
            row, text="Validate source model", command=lambda: self.guard(self.validate)
        ).pack(side="left")
        ttk.Button(row, text="Inspect live unit dependencies", command=self.inspect_live).pack(
            side="left", padx=4
        )
        self.message = ttk.Label(
            outer,
            text="Source editing never starts/stops services. Apply generated drop-ins outside devdash; explicitly reload Core.",
            wraplength=980,
        )
        self.message.pack(fill="x", pady=6)
        tabs = ttk.Notebook(outer)
        tabs.pack(fill="both", expand=True)
        work, stages, deps, result = [ttk.Frame(tabs, padding=8) for _ in range(4)]
        for frame, title in zip(
            (work, stages, deps, result),
            ("Workloads", "Stages", "Dependencies", "Generation / Diff"),
        ):
            tabs.add(frame, text=title)
        self.work_list = ttk.Treeview(
            work, columns=("scope", "unit"), show="tree headings", height=6
        )
        self.work_list.heading("#0", text="Logical ID")
        self.work_list.heading("scope", text="Lifecycle")
        self.work_list.heading("unit", text="Unit")
        self.work_list.pack(fill="x")
        self.work_list.bind("<<TreeviewSelect>>", self.select_workload)
        self.work_fields = {}
        for key, values in (
            ("id", None),
            ("name", None),
            ("backend", ("systemd", "docker")),
            ("scope", ("user", "system")),
            ("unit", None),
            ("stage", ()),
        ):
            row = ttk.Frame(work)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=key, width=12).pack(side="left")
            widget = (
                ttk.Combobox(row, values=values, state="readonly")
                if values is not None
                else ttk.Entry(row)
            )
            widget.pack(side="left", fill="x", expand=True)
            self.work_fields[key] = widget
        self.work_fields["backend"].set("systemd")
        self.work_fields["scope"].set("user")
        ttk.Label(
            work,
            text="Optional docker / observer / controller settings (YAML; existing service ExecStart stays in unit)",
        ).pack(fill="x")
        self.advanced = ScrolledText(work, height=6)
        self.advanced.pack(fill="both", expand=True)
        row = ttk.Frame(work)
        row.pack(fill="x", pady=5)
        for label, command in (
            ("New", self.new_workload),
            ("Apply Workload", self.apply_workload),
            ("Delete Workload + references", self.delete_workload),
            ("Scan existing units", self.scan),
        ):
            ttk.Button(row, text=label, command=lambda fn=command: self.guard(fn)).pack(
                side="left", padx=3
            )
        self.stage_list = ttk.Treeview(stages, show="tree", height=6)
        self.stage_list.pack(fill="x")
        self.stage_list.bind("<<TreeviewSelect>>", self.select_stage)
        self.stage_fields = {}
        for key in ("id", "name", "description"):
            row = ttk.Frame(stages)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=key, width=12).pack(side="left")
            widget = ttk.Entry(row)
            widget.pack(side="left", fill="x", expand=True)
            self.stage_fields[key] = widget
        ttk.Label(
            stages,
            text="Members (Ctrl/Shift multi-select; each Workload belongs to at most one Stage)",
        ).pack(fill="x")
        self.members = ttk.Treeview(stages, show="tree", selectmode="extended", height=5)
        self.members.pack(fill="x")
        ttk.Label(
            stages, text="Developer checks (one per line; observations only, no approval latch)"
        ).pack(fill="x")
        self.checks = ScrolledText(stages, height=3)
        self.checks.pack(fill="both", expand=True)
        row = ttk.Frame(stages)
        row.pack(fill="x", pady=5)
        for label, command in (
            ("New", self.new_stage),
            ("Apply Stage", self.apply_stage),
            ("Delete Stage", self.delete_stage),
            ("Up", lambda: self.move_stage(-1)),
            ("Down", lambda: self.move_stage(1)),
        ):
            ttk.Button(row, text=label, command=lambda fn=command: self.guard(fn)).pack(
                side="left", padx=3
            )
        ttk.Label(
            deps,
            text="Dependent Workload → required Workloads in earlier Stages. Generates additive Requisite= and After= in the same manager only.",
            wraplength=950,
        ).pack(fill="x")
        self.dependent = ttk.Combobox(deps, state="readonly")
        self.dependent.pack(fill="x", pady=5)
        self.dependent.bind("<<ComboboxSelected>>", self.select_dependencies)
        self.prerequisites = ttk.Treeview(deps, show="tree", selectmode="extended")
        self.prerequisites.pack(fill="both", expand=True)
        ttk.Button(
            deps,
            text="Apply explicit dependencies",
            command=lambda: self.guard(self.apply_dependencies),
        ).pack(fill="x")
        self.result = ScrolledText(result, wrap="none")
        self.result.pack(fill="both", expand=True)
        if source:
            self.guard(self.open_source)
        self.refresh()

    def guard(self, operation):
        try:
            return operation()
        except (OSError, ValueError, RuntimeError, yaml.YAMLError) as error:
            self.message.configure(text=str(error))
            return None

    def show(self, text):
        if self.closed:
            return
        self.result.delete("1.0", "end")
        self.result.insert("1.0", text)

    def refresh(self):
        for tree in (self.work_list, self.stage_list, self.members, self.prerequisites):
            tree.delete(*tree.get_children())
        for key, work in self.config.workloads.items():
            self.work_list.insert(
                "",
                "end",
                iid=key,
                text=key,
                values=(work["backend"] + ":" + work["scope"], work["unit"]),
            )
            for tree in (self.members, self.prerequisites):
                tree.insert("", "end", iid=key, text=key)
        for stage in self.config.stages:
            self.stage_list.insert(
                "",
                "end",
                iid=stage["id"],
                text=stage["name"] + " · " + ", ".join(stage["workloads"]),
            )
        self.work_fields["stage"].configure(
            values=["", *[stage["id"] for stage in self.config.stages]]
        )
        self.dependent.configure(values=list(self.config.workloads))

    def replace(self, raw):
        candidate = Config(raw)
        self.config = candidate
        self.refresh()
        self.message.configure(
            text="Model updated locally. Save / Preview / Generate are explicit; Core unchanged."
        )

    @staticmethod
    def put(widget, text):
        if isinstance(widget, ttk.Combobox):
            widget.set(text)
        else:
            widget.delete(0, "end")
            widget.insert(0, text)

    def new_workload(self):
        self.work_list.selection_remove(*self.work_list.selection())
        for key, widget in self.work_fields.items():
            self.put(widget, "systemd" if key == "backend" else "user" if key == "scope" else "")
        self.advanced.delete("1.0", "end")

    def select_workload(self, event=None):
        selected = self.work_list.selection()
        if not selected:
            return
        key = selected[0]
        work = self.config.workloads[key]
        for name, widget in self.work_fields.items():
            self.put(
                widget,
                key
                if name == "id"
                else self.config.stages[self.config.membership[key]]["id"]
                if name == "stage" and key in self.config.membership
                else ""
                if name == "stage"
                else work.get(name, ""),
            )
        self.advanced.delete("1.0", "end")
        self.advanced.insert(
            "1.0",
            yaml.safe_dump(
                {k: v for k, v in work.items() if k in ("docker", "observer", "controller")},
                sort_keys=False,
            ),
        )

    def apply_workload(self):
        values = {key: widget.get() for key, widget in self.work_fields.items()}
        key, stage = values.pop("id"), values.pop("stage")
        if not key:
            raise ValueError("Workload ID is required")
        extra = parse_yaml(self.advanced.get("1.0", "end")) or {}
        fields(extra, {"docker", "observer", "controller"}, "advanced settings")
        values.update(extra)
        raw = self.config.json()
        selected = self.work_list.selection()
        if not selected and key in raw["workloads"]:
            raise ValueError("Workload ID already exists; select it to edit")
        old = selected[0] if selected else key
        if key != old and key in raw["workloads"]:
            raise ValueError("Workload ID already exists")
        raw["workloads"].pop(old, None)
        raw["workloads"][key] = values
        if old in raw["dependencies"]:
            raw["dependencies"][key] = raw["dependencies"].pop(old)
        raw["dependencies"] = {
            name: [key if p == old else p for p in refs]
            for name, refs in raw["dependencies"].items()
        }
        for item in raw["stages"]:
            item["workloads"] = [member for member in item["workloads"] if member != old]
            if item["id"] == stage:
                item["workloads"].append(key)
        self.replace(raw)

    def delete_workload(self):
        if not self.work_list.selection():
            return
        key = self.work_list.selection()[0]
        raw = self.config.json()
        raw["workloads"].pop(key)
        raw["dependencies"].pop(key, None)
        for refs in raw["dependencies"].values():
            if key in refs:
                refs.remove(key)
        for stage in raw["stages"]:
            stage["workloads"] = [member for member in stage["workloads"] if member != key]
        self.replace(raw)

    def new_stage(self):
        self.stage_list.selection_remove(*self.stage_list.selection())
        for widget in self.stage_fields.values():
            self.put(widget, "")
        self.members.selection_remove(*self.members.selection())
        self.checks.delete("1.0", "end")

    def select_stage(self, event=None):
        if not self.stage_list.selection():
            return
        stage = self.config.stage(self.stage_list.selection()[0])
        for key, widget in self.stage_fields.items():
            self.put(widget, stage[key])
        self.members.selection_set(stage["workloads"])
        self.checks.delete("1.0", "end")
        self.checks.insert("1.0", "\n".join(stage["checks"]))

    def apply_stage(self):
        stage = {key: widget.get() for key, widget in self.stage_fields.items()}
        stage.update(
            workloads=list(self.members.selection()),
            checks=[line for line in self.checks.get("1.0", "end").splitlines() if line.strip()],
        )
        raw = self.config.json()
        selected = self.stage_list.selection()
        index = next(
            (i for i, value in enumerate(raw["stages"]) if selected and value["id"] == selected[0]),
            len(raw["stages"]),
        )
        if index == len(raw["stages"]):
            raw["stages"].append(stage)
        else:
            raw["stages"][index] = stage
        self.replace(raw)

    def delete_stage(self):
        if self.stage_list.selection():
            raw = self.config.json()
            raw["stages"] = [
                stage for stage in raw["stages"] if stage["id"] != self.stage_list.selection()[0]
            ]
            self.replace(raw)

    def move_stage(self, offset):
        if not self.stage_list.selection():
            return
        key = self.stage_list.selection()[0]
        index = next(i for i, stage in enumerate(self.config.stages) if stage["id"] == key)
        other = index + offset
        if 0 <= other < len(self.config.stages):
            raw = self.config.json()
            raw["stages"][index], raw["stages"][other] = raw["stages"][other], raw["stages"][index]
            self.replace(raw)
            self.stage_list.selection_set(key)

    def select_dependencies(self, event=None):
        self.prerequisites.selection_set(self.config.dependencies.get(self.dependent.get(), []))

    def apply_dependencies(self):
        raw = self.config.json()
        raw["dependencies"][self.dependent.get()] = list(self.prerequisites.selection())
        self.replace(raw)

    def open_source(self):
        path = self.source.get() or filedialog.askopenfilename(
            parent=self, filetypes=[("YAML", "*.yaml *.yml")]
        )
        if not path:
            return
        text = Path(path).read_text()
        self.config = Config.parse(text)
        self.loaded_path, self.expected = Path(path).absolute(), text
        self.source.set(path)
        self.refresh()
        self.message.configure(text="Source loaded. Runtime configuration was not changed.")

    def save(self):
        path = self.source.get() or filedialog.asksaveasfilename(
            parent=self, defaultextension=".yaml"
        )
        if not path:
            raise ValueError("Select a source path")
        self.expected = save_source(
            path, self.config, self.expected if Path(path).absolute() == self.loaded_path else None
        )
        self.source.set(path)
        self.loaded_path = Path(path).absolute()
        self.message.configure(
            text="Source saved; Core unchanged. Generate, review/apply drop-ins, then explicitly reload Core."
        )

    def validate(self):
        self.config = Config(self.config.json())
        self.message.configure(
            text="Valid source model. Existing host dependencies require live inspection too."
        )

    def choose_output(self):
        if path := filedialog.askdirectory(parent=self):
            self.output.set(path)

    def preview(self):
        if not self.output.get():
            raise ValueError("Choose an output directory")
        self.plan = preview(self.config, self.output.get())
        self.show(
            "\n\n".join(
                f"{item['action']}: {item['path']}\n{item['diff']}" for item in self.plan["changes"]
            )
        )
        return self.plan

    def generate(self):
        if not self.output.get():
            raise ValueError("Choose an output directory")
        self.save()
        self.plan = generate(self.config, self.output.get())
        self.show(json.dumps(self.plan, indent=2, ensure_ascii=False))
        self.message.configure(
            text="Generated current bundle atomically. No units applied/reloaded; Core unchanged."
        )
        return self.plan

    def inspect_live(self):
        self.owner.send(
            dict(command="inspect_config", config=copy.deepcopy(self.config.json())),
            lambda report: self.show(json.dumps(report, indent=2)),
        )

    def scan(self):
        dialog = ttk.Toplevel(title="Scan existing units → editor candidate", size=(650, 500))
        scope = ttk.Combobox(dialog, values=("user", "system"), state="readonly")
        scope.set(self.work_fields["scope"].get() or "user")
        scope.pack(fill="x")
        units = ttk.Treeview(dialog, show="tree", selectmode="browse")
        units.pack(fill="both", expand=True)

        def clear(event=None):
            units.delete(*units.get_children())

        scope.bind("<<ComboboxSelected>>", clear)

        def scan():
            clear()
            requested_scope = scope.get()

            def received(values):
                if dialog.winfo_exists() and scope.get() == requested_scope:
                    for unit in values:
                        if not units.exists(unit):
                            units.insert("", "end", iid=unit, text=unit)

            self.owner.send(dict(command="scan", scope=requested_scope), received)

        def select():
            if not units.selection():
                return
            unit, manager = units.selection()[0], scope.get()

            def resolved(value):
                if not self.closed:
                    self.new_workload()
                    for key, val in dict(
                        id=unit.removesuffix(".service"),
                        backend="systemd",
                        scope=manager,
                        unit=unit,
                    ).items():
                        self.put(self.work_fields[key], val)
                    self.message.configure(
                        text="Scan candidate selected; Apply Workload edits source model only."
                    )

            self.owner.send(dict(command="resolve", scope=manager, unit=unit), resolved)

        ttk.Button(dialog, text="Scan", command=scan).pack(fill="x")
        ttk.Button(dialog, text="Use selected unit as candidate", command=select).pack(fill="x")
        return dialog

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.destroy()
        self.source = self.output = None  # Release Tcl variables on their owning thread.
