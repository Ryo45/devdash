use devdash::{
    children::{ChildAction, ChildKind},
    engine::{Action, Observation},
    ipc::{self, Request, Snapshot},
    model::{Config, Scope, Step},
};
use gpui_kit::{
    component::{
        button::*,
        list::ListItem,
        tree::{TreeItem, TreeState, tree},
        *,
    },
    *,
};
use std::{collections::BTreeSet, path::PathBuf, sync::mpsc, time::Duration};

enum Update {
    Snapshot(Box<Snapshot>),
    Scan { scope: Scope, units: Vec<String> },
    Logs { workload: String, text: String },
    Message(String),
    Error(String),
}
struct Dashboard {
    tree: Entity<TreeState>,
    snapshot: Option<Snapshot>,
    selected: Option<(String, bool)>,
    selected_child: Option<String>,
    commands: tokio::sync::mpsc::UnboundedSender<Request>,
    updates: mpsc::Receiver<Update>,
    logs: String,
    message: String,
    connection_error: Option<String>,
    scan: Option<(Scope, Vec<String>)>,
    pending_imports: BTreeSet<String>,
}
fn worker(
    socket: PathBuf,
) -> (
    tokio::sync::mpsc::UnboundedSender<Request>,
    mpsc::Receiver<Update>,
) {
    let (commands, mut requests) = tokio::sync::mpsc::unbounded_channel::<Request>();
    let (updates, receiver) = mpsc::channel();
    std::thread::spawn(move || {
        let runtime = tokio::runtime::Runtime::new().expect("Tokio runtime");
        runtime.block_on(async move {
            let poll_socket = socket.clone();
            let poll_updates = updates.clone();
            let polling = tokio::spawn(async move {
                loop {
                    let result = tokio::time::timeout(
                        Duration::from_secs(4),
                        ipc::request(&poll_socket, Request::Snapshot),
                    )
                    .await;
                    let update = match result {
                        Ok(Ok(r)) => match serde_json::from_value::<Snapshot>(r.data) {
                            Ok(s) => Update::Snapshot(Box::new(s)),
                            Err(e) => Update::Error(e.to_string()),
                        },
                        Ok(Err(e)) => Update::Error(format!("Core: {e:#}")),
                        Err(_) => Update::Error("Core connection timed out".into()),
                    };
                    if poll_updates.send(update).is_err() {
                        break;
                    }
                    tokio::time::sleep(Duration::from_millis(500)).await;
                }
            });
            while let Some(req) = requests.recv().await {
                let socket = socket.clone();
                let updates = updates.clone();
                tokio::spawn(async move {
                    let result = tokio::time::timeout(
                        Duration::from_secs(8),
                        ipc::request(&socket, req.clone()),
                    )
                    .await;
                    let update = match result {
                        Ok(Ok(r)) => match req {
                            Request::Scan { scope } => Update::Scan {
                                scope,
                                units: serde_json::from_value(r.data).unwrap_or_default(),
                            },
                            Request::Logs { workload } => Update::Logs {
                                workload,
                                text: r.data.as_str().unwrap_or_default().into(),
                            },
                            Request::Inspect { .. } => {
                                Update::Message("Dependency inspection refreshed".into())
                            }
                            _ => Update::Message(
                                r.data.as_str().unwrap_or("Request accepted").into(),
                            ),
                        },
                        Ok(Err(e)) => Update::Message(format!("Error: {e:#}")),
                        Err(_) => Update::Message(
                            "Request timed out; check core status before retrying".into(),
                        ),
                    };
                    let _ = updates.send(update);
                });
            }
            polling.abort();
        });
    });
    (commands, receiver)
}
fn tree_items(
    c: &Config,
    observations: &std::collections::BTreeMap<String, Observation>,
) -> Vec<TreeItem> {
    fn group(
        c: &Config,
        observations: &std::collections::BTreeMap<String, Observation>,
        id: &str,
    ) -> TreeItem {
        let g = &c.groups[id];
        let mut children = Vec::new();
        for s in &g.steps {
            match s {
                Step::Group(g) => children.push(group(c, observations, g)),
                Step::Workload(w) => children.push(workload(c, observations, w)),
                Step::Parallel(ws) => {
                    children.extend(ws.iter().map(|w| workload(c, observations, w)))
                }
                Step::Gate(g) => {
                    children.push(TreeItem::new(format!("t:{g}"), format!("Gate · {g}")))
                }
            }
        }
        TreeItem::new(
            format!("g:{id}"),
            if g.name.is_empty() {
                id.to_string()
            } else {
                g.name.clone()
            },
        )
        .children(children)
        .expanded(true)
    }
    fn workload(
        c: &Config,
        observations: &std::collections::BTreeMap<String, Observation>,
        id: &str,
    ) -> TreeItem {
        let w = &c.workloads[id];
        TreeItem::new(
            format!("w:{id}"),
            if w.name.is_empty() {
                id.to_string()
            } else {
                w.name.clone()
            } + &format!(
                " [{}]",
                if w.backend == devdash::model::BackendKind::Docker {
                    "docker".to_owned()
                } else {
                    w.scope.to_string()
                }
            ),
        )
        .children(
            observations
                .get(id)
                .into_iter()
                .flat_map(|o| o.children.iter())
                .filter(|child| child.kind != ChildKind::Process && child.parent.is_none())
                .map(|child| {
                    let key = |child: &devdash::children::ChildObservation| {
                        TreeItem::new(
                            format!("c:{}", serde_json::to_string(&(id, &child.id)).unwrap()),
                            format!("{:?} · {}", child.kind, child.name),
                        )
                    };
                    key(child)
                        .children(
                            observations[id]
                                .children
                                .iter()
                                .filter(|node| node.parent.as_deref() == Some(child.id.as_str()))
                                .map(key),
                        )
                        .expanded(true)
                }),
        )
        .expanded(true)
    }
    let mut child_groups = BTreeSet::new();
    let mut child_workloads = BTreeSet::new();
    for g in c.groups.values() {
        for s in &g.steps {
            match s {
                Step::Group(g) => {
                    child_groups.insert(g.clone());
                }
                Step::Workload(w) => {
                    child_workloads.insert(w.clone());
                }
                Step::Parallel(ws) => child_workloads.extend(ws.iter().cloned()),
                _ => {}
            }
        }
    }
    let mut items: Vec<_> = c
        .groups
        .keys()
        .filter(|id| !child_groups.contains(*id))
        .map(|id| group(c, observations, id))
        .collect();
    items.extend(
        c.workloads
            .keys()
            .filter(|id| !child_workloads.contains(*id))
            .map(|id| workload(c, observations, id)),
    );
    items
}
impl Dashboard {
    fn new(socket: PathBuf, cx: &mut Context<Self>) -> Self {
        let (commands, updates) = worker(socket);
        cx.spawn(async move |view, cx| {
            loop {
                cx.background_executor()
                    .timer(Duration::from_millis(150))
                    .await;
                if view
                    .update(cx, |this, cx| {
                        this.receive(cx);
                        cx.notify();
                    })
                    .is_err()
                {
                    break;
                }
            }
        })
        .detach();
        Self {
            tree: cx.new(|cx| TreeState::new(cx)),
            snapshot: None,
            selected: None,
            selected_child: None,
            commands,
            updates,
            logs: "Select a service to view its journal.".into(),
            message: "Connecting to headless core…".into(),
            connection_error: None,
            scan: None,
            pending_imports: BTreeSet::new(),
        }
    }
    fn send(&self, req: Request) {
        let _ = self.commands.send(req);
    }
    fn receive(&mut self, cx: &mut Context<Self>) {
        while let Ok(update) = self.updates.try_recv() {
            match update {
                Update::Snapshot(s) => {
                    let config_changed = self
                        .snapshot
                        .as_ref()
                        .is_none_or(|previous| previous.config != s.config);
                    let keys = |snapshot: &Snapshot| {
                        snapshot
                            .observations
                            .iter()
                            .flat_map(|(parent, o)| {
                                o.children
                                    .iter()
                                    .filter(|child| child.kind != ChildKind::Process)
                                    .map(move |child| {
                                        (parent.clone(), child.id.clone(), child.epoch.clone())
                                    })
                            })
                            .collect::<Vec<_>>()
                    };
                    let children_changed = self
                        .snapshot
                        .as_ref()
                        .is_none_or(|previous| keys(previous) != keys(&s));
                    if config_changed || children_changed {
                        let stale_child = self
                            .selected
                            .as_ref()
                            .zip(self.selected_child.as_ref())
                            .is_some_and(|((parent, _), id)| {
                                let epoch = |snapshot: &Snapshot| {
                                    snapshot
                                        .observations
                                        .get(parent)
                                        .and_then(|o| o.children.iter().find(|c| &c.id == id))
                                        .map(|c| c.epoch.clone())
                                };
                                self.snapshot
                                    .as_ref()
                                    .is_some_and(|previous| epoch(previous) != epoch(&s))
                            });
                        let selected = if stale_child {
                            None
                        } else {
                            self.tree.read(cx).selected_item().cloned()
                        };
                        if stale_child {
                            self.selected = None;
                            self.selected_child = None;
                            self.message =
                                "Child target changed; select it again before operating".into();
                        }
                        if config_changed {
                            self.selected = None;
                            self.selected_child = None;
                        }
                        let items = tree_items(&s.config, &s.observations);
                        self.tree.update(cx, |tree, cx| {
                            tree.set_items(items, cx);
                            if !config_changed {
                                tree.set_selected_item(selected.as_ref(), cx);
                            }
                        });
                        if config_changed {
                            self.message = "Connected · local core".into();
                        }
                    }
                    self.connection_error = None;
                    self.snapshot = Some(*s);
                }
                Update::Scan { scope, units } => self.scan = Some((scope, units)),
                Update::Logs { workload, text } => {
                    if self.selected.as_ref() == Some(&(workload, false)) {
                        self.logs = text;
                    }
                }
                Update::Message(m) => {
                    if m.starts_with("Error:") || m.starts_with("Request timed out") {
                        self.pending_imports.clear();
                    }
                    self.message = m;
                }
                Update::Error(e) => self.connection_error = Some(e),
            }
        }
    }
    fn operate(&self, action: Action) {
        if let (Some((parent, false)), Some(id), Some(snapshot)) =
            (&self.selected, &self.selected_child, &self.snapshot)
            && let Some(child) = snapshot
                .observations
                .get(parent)
                .and_then(|o| o.children.iter().find(|c| &c.id == id))
        {
            self.send(Request::OperateChild {
                parent: parent.clone(),
                child: id.clone(),
                epoch: child.epoch.clone(),
                action,
            });
            return;
        }
        if let Some((target, group)) = &self.selected {
            self.send(Request::Operate {
                target: target.clone(),
                group: *group,
                action,
            });
        }
    }
}
impl Render for Dashboard {
    fn render(&mut self, _: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let entry = self
            .tree
            .read(cx)
            .selected_entry()
            .map(|e| e.item().id.to_string());
        if let Some(key) = entry.as_ref().and_then(|key| key.strip_prefix("c:"))
            && let Ok((parent, child)) = serde_json::from_str::<(String, String)>(key)
        {
            if self.selected.as_ref() != Some(&(parent.clone(), false))
                || self.selected_child.as_ref() != Some(&child)
            {
                self.logs.clear();
                self.send(Request::Logs {
                    workload: parent.clone(),
                });
            }
            self.selected = Some((parent, false));
            self.selected_child = Some(child);
        }
        if let Some(key) = entry
            && let Some((kind, id)) = key.split_once(':')
            && (kind == "g" || kind == "w")
        {
            self.selected_child = None;
            let selected = Some((id.to_string(), kind == "g"));
            if self.selected != selected {
                self.selected = selected;
                self.logs.clear();
                if kind == "w" {
                    self.send(Request::Logs {
                        workload: id.into(),
                    });
                }
            }
        }
        if self
            .tree
            .read(cx)
            .selected_entry()
            .is_some_and(|e| e.item().id.starts_with("t:"))
        {
            self.selected = None;
            self.selected_child = None;
            self.logs =
                "Gates belong to group sequences. Use Approve Ready when a Manual Gate is waiting."
                    .into();
        }
        let busy = self
            .snapshot
            .as_ref()
            .is_some_and(|s| matches!(s.run.status.as_str(), "running" | "cancelling"));
        let disconnected = self.connection_error.is_some() || self.snapshot.is_none();
        let has_selection = self.selected.is_some();
        let is_group = self.selected.as_ref().is_some_and(|s| s.1);
        let manager_unavailable = self.snapshot.as_ref().is_some_and(|snapshot| {
            let Some((id, group)) = &self.selected else {
                return false;
            };
            let affected = if *group {
                snapshot.config.expand(id, false).unwrap_or_default()
            } else {
                vec![Step::Workload(id.clone())]
            };
            affected
                .iter()
                .flat_map(|step| match step {
                    Step::Workload(id) => std::slice::from_ref(id),
                    Step::Parallel(ids) => ids.as_slice(),
                    _ => &[],
                })
                .filter(|id| {
                    snapshot.config.workloads[*id].backend == devdash::model::BackendKind::Systemd
                })
                .any(|id| {
                    snapshot
                        .monitoring
                        .get(&snapshot.config.workloads[id].scope)
                        .is_some_and(|health| !health.available)
                })
        });
        let mut toolbar = h_flex()
            .gap_2()
            .child(
                div()
                    .text_xl()
                    .font_weight(FontWeight::BOLD)
                    .child("devdash"),
            )
            .child(div().flex_1());
        toolbar = toolbar
            .child(
                Button::new("scan")
                    .label("Scan user")
                    .disabled(disconnected)
                    .on_click(cx.listener(|this, _, _, _| {
                        this.send(Request::Scan { scope: Scope::User });
                    })),
            )
            .child(
                Button::new("scan-system")
                    .label("Scan system")
                    .disabled(disconnected)
                    .on_click(cx.listener(|this, _, _, _| {
                        this.send(Request::Scan {
                            scope: Scope::System,
                        })
                    })),
            )
            .child(
                Button::new("cancel")
                    .label("Cancel sequence")
                    .disabled(!busy || disconnected)
                    .on_click(cx.listener(|this, _, _, _| this.send(Request::Cancel))),
            );
        let mut actions = h_flex().gap_2();
        for (id, label, action) in [
            ("start", "Start", Action::Start),
            ("stop", "Stop", Action::Stop),
            ("restart", "Restart", Action::Restart),
            ("reinitialize", "Reinitialize", Action::Reinitialize),
        ] {
            actions = actions.child(
                Button::new(id)
                    .label(label)
                    .disabled(
                        disconnected
                            || manager_unavailable
                            || busy
                            || self.selected_child.as_ref().is_some_and(|id| {
                                let capability = match action {
                                    Action::Start => Some(ChildAction::Start),
                                    Action::Stop => Some(ChildAction::Stop),
                                    Action::Restart => Some(ChildAction::Restart),
                                    Action::Reinitialize => None,
                                };
                                !self
                                    .snapshot
                                    .as_ref()
                                    .and_then(|s| {
                                        self.selected
                                            .as_ref()
                                            .and_then(|(parent, _)| s.observations.get(parent))
                                    })
                                    .and_then(|o| o.children.iter().find(|c| &c.id == id))
                                    .is_some_and(|child| {
                                        capability.is_some_and(|c| child.actions.contains(&c))
                                    })
                            })
                            || !has_selection
                            || (action == Action::Reinitialize && !is_group),
                    )
                    .on_click(cx.listener(move |this, _, _, _| this.operate(action))),
            );
        }
        let mut detail = v_flex()
            .gap_3()
            .child(
                div().text_xl().child(
                    self.selected
                        .as_ref()
                        .map(|(id, g)| {
                            format!(
                                "{} · {}",
                                if self.selected_child.is_some() {
                                    "Child"
                                } else if *g {
                                    "Group"
                                } else {
                                    "Workload"
                                },
                                self.selected_child.as_deref().unwrap_or(id)
                            )
                        })
                        .unwrap_or("Select a group or service".into()),
                ),
            )
            .child(actions);
        if let Some(s) = &self.snapshot {
            detail = detail.child(div().child(format!(
                "Sequence: {}  {}",
                s.run.target,
                if s.run.status.is_empty() {
                    "idle"
                } else {
                    &s.run.status
                }
            )));
            if let Some(gate) = &s.run.waiting_gate {
                let mut gate_row = h_flex()
                    .gap_3()
                    .p_3()
                    .bg(rgb(0x39402a))
                    .rounded_md()
                    .child(format!("Waiting for gate: {gate}"));
                if matches!(s.config.gates.get(gate), Some(devdash::model::Gate::Manual)) {
                    let gate = gate.clone();
                    gate_row = gate_row.child(
                        Button::new("approve")
                            .label("Approve Ready")
                            .primary()
                            .disabled(disconnected)
                            .on_click(cx.listener(move |this, _, _, _| {
                                this.send(Request::Approve { gate: gate.clone() })
                            })),
                    );
                }
                detail = detail.child(gate_row);
            }
            if let Some((id, group)) = &self.selected {
                let ids: Vec<_> = if *group {
                    s.config
                        .expand(id, false)
                        .unwrap_or_default()
                        .into_iter()
                        .flat_map(|step| match step {
                            Step::Workload(w) => vec![w],
                            Step::Parallel(ws) => ws,
                            _ => vec![],
                        })
                        .collect()
                } else {
                    vec![id.clone()]
                };
                for id in ids {
                    if let Some(w) = s.config.workloads.get(&id) {
                        let o = s.observations.get(&id);
                        detail = detail.child(div().p_2().rounded_md().bg(rgb(0x202936)).child(
                            format!(
                                "{}   {} / {}   {}   PID {}   Memory {}   CPU {}",
                                id,
                                o.map(|o| o.state.as_str()).unwrap_or("unknown"),
                                o.map(|o| o.sub_state.as_str()).unwrap_or(""),
                                w.display_reference(),
                                o.and_then(|o| o.pid)
                                    .map(|n| n.to_string())
                                    .unwrap_or("—".into()),
                                o.and_then(|o| o.memory_bytes)
                                    .map(|n| format!("{:.1} MiB", n as f64 / 1048576.))
                                    .unwrap_or("—".into()),
                                o.and_then(|o| o.cpu_nsec)
                                    .map(|n| format!("{:.2}s", n as f64 / 1e9))
                                    .unwrap_or("—".into())
                            ),
                        ));
                        if let Some(observation) = o {
                            if let Some(health) = &observation.health {
                                detail = detail.child(format!(
                                    "Container health: {health} (Docker Healthcheck)"
                                ));
                            }
                            if let Some(error) = &observation.children_error {
                                detail = detail
                                    .child(div().text_color(rgb(0xe6c07b)).child(error.clone()));
                            }
                            for child in observation
                                .children
                                .iter()
                                .filter(|child| {
                                    self.selected_child
                                        .as_ref()
                                        .is_none_or(|selected| selected == &child.id)
                                })
                                .take(100)
                            {
                                let mut row =
                                    v_flex().gap_1().p_2().bg(rgb(0x181e28)).child(format!(
                                        "{:?} · {} · {} · PID {} ({}) · CPU avg {} · RSS {} · runtime {}",
                                        child.kind,
                                        child.name,
                                        child.state,
                                        child.pid.map(|p| p.to_string()).unwrap_or("—".into()),
                                        child.pid_namespace,
                                        child
                                            .cpu_percent
                                            .map(|p| format!("{p:.1}%"))
                                            .unwrap_or("—".into()),
                                        child
                                            .memory_bytes
                                            .map(|m| format!("{:.1} MiB", m as f64 / 1048576.))
                                            .unwrap_or("—".into()),
                                        child.runtime_id.as_deref().unwrap_or("—")
                                    ));
                                if let Some(reason) = &child.reason {
                                    row = row.child(reason.clone());
                                }
                                let mut buttons = h_flex().gap_2();
                                for (name, action, capability) in [
                                    ("Start", Action::Start, ChildAction::Start),
                                    ("Stop", Action::Stop, ChildAction::Stop),
                                    ("Restart", Action::Restart, ChildAction::Restart),
                                ] {
                                    let parent = id.clone();
                                    let child_id = child.id.clone();
                                    let epoch = child.epoch.clone();
                                    buttons = buttons.child(
                                        Button::new(format!("child-{}-{name}", child.id))
                                            .label(name)
                                            .disabled(
                                                disconnected
                                                    || busy
                                                    || !child.actions.contains(&capability),
                                            )
                                            .on_click(cx.listener(move |this, _, _, _| {
                                                this.send(Request::OperateChild {
                                                    parent: parent.clone(),
                                                    child: child_id.clone(),
                                                    epoch: epoch.clone(),
                                                    action,
                                                })
                                            })),
                                    );
                                }
                                detail = detail.child(row.child(buttons));
                            }
                        }
                        if let Some(error) = o.and_then(|o| o.error.as_ref()) {
                            detail =
                                detail.child(div().text_color(rgb(0xf0a1a1)).child(error.clone()));
                        }
                    }
                }
            }
            let mut progress = v_flex().gap_1();
            for p in s.run.progress.iter().rev().take(100) {
                progress = progress.child(format!(
                    "{:?}  {}  {}{}",
                    p.action,
                    p.step,
                    p.status,
                    p.error
                        .as_ref()
                        .map(|e| format!(" · {e}"))
                        .unwrap_or_default()
                ));
            }
            if !s.run.progress.is_empty() {
                detail = detail.child(
                    div()
                        .id("progress")
                        .h(px(200.))
                        .overflow_y_scroll()
                        .child(progress),
                );
            }
            if let Some((id, true)) = &self.selected {
                let group = id.clone();
                detail = detail.child(
                    Button::new("inspect")
                        .label("Check dependencies")
                        .disabled(disconnected || busy)
                        .on_click(cx.listener(move |this, _, _, _| {
                            this.send(Request::Inspect {
                                group: group.clone(),
                                action: Action::Restart,
                            })
                        })),
                );
                if let Some(report) = s.inspections.get(id) {
                    detail = detail.child(format!(
                        "Dependency inspection: {}, {} units{}; rechecked before operations",
                        report
                            .action
                            .map(|a| format!("{a:?}"))
                            .unwrap_or_else(|| "Pending".into()),
                        report.inspected_units,
                        if report.stale {
                            " · STALE: refresh pending"
                        } else {
                            ""
                        }
                    ));
                    for d in &report.diagnostics {
                        detail = detail.child(
                            div()
                                .text_color(if d.level == "error" {
                                    rgb(0xf0a1a1)
                                } else {
                                    rgb(0xe6c07b)
                                })
                                .child(format!(
                                    "{} [{}] {}{}",
                                    d.level,
                                    d.code,
                                    d.message,
                                    if d.path.is_empty() {
                                        String::new()
                                    } else {
                                        format!(" · {}", d.path.join(" / "))
                                    }
                                )),
                        );
                    }
                    for reason in &report.incomplete {
                        detail = detail.child(
                            div()
                                .text_color(rgb(0xe6c07b))
                                .child(format!("Inspection incomplete: {reason}")),
                        );
                    }
                }
            }
            let mut events = v_flex()
                .gap_1()
                .child("Observed events · origin attribution is best effort");
            for event in s.events.iter().rev().take(30) {
                let seconds = event.observed_unix_ms / 1000;
                events = events.child(
                    div()
                        .text_color(if event.level == "warning" {
                            rgb(0xe6c07b)
                        } else {
                            rgb(0x9baac1)
                        })
                        .child(format!(
                            "{:02}:{:02}:{:02} UTC #{} {} {} {}",
                            seconds / 3600 % 24,
                            seconds / 60 % 60,
                            seconds % 60,
                            event.id,
                            event.level,
                            event.workload.as_deref().unwrap_or("manager"),
                            event.message
                        )),
                );
            }
            detail = detail.child(
                div()
                    .id("events")
                    .max_h(px(160.))
                    .overflow_y_scroll()
                    .child(events),
            );
        }
        let mut content = h_flex()
            .items_stretch()
            .overflow_hidden()
            .flex_1()
            .min_h_0()
            .gap_4()
            .child(
                div()
                    .w(px(260.))
                    .h_full()
                    .flex_shrink_0()
                    .rounded_md()
                    .bg(rgb(0x181e28))
                    .p_2()
                    .child(tree(&self.tree, |ix, entry, selected, _, _| {
                        let marker = if entry.is_folder() {
                            if entry.is_expanded() { "▾" } else { "▸" }
                        } else if entry.item().id.starts_with("t:") {
                            "◇"
                        } else {
                            "·"
                        };
                        ListItem::new(ix)
                            .selected(selected)
                            .pl(px(12.) + px(16.) * entry.depth() as f32)
                            .child(format!("{marker} {}", entry.item().label))
                    })),
            )
            .child(
                div()
                    .id("detail")
                    .h_full()
                    .flex_1()
                    .min_w_0()
                    .overflow_y_scroll()
                    .child(detail),
            );
        if let Some((scope, units)) = self.scan.clone() {
            let mut rows = v_flex().gap_2().child(
                h_flex()
                    .gap_2()
                    .child(format!("Import systemd {scope} units"))
                    .child(
                        Button::new("close-scan")
                            .label("Close")
                            .on_click(cx.listener(|this, _, _, _| this.scan = None)),
                    ),
            );
            rows = rows.child("Import saves YAML; restart core to load changes.");
            for (i, unit) in units.into_iter().enumerate() {
                let registered = self.pending_imports.contains(&format!("{scope}:{unit}"))
                    || self.snapshot.as_ref().is_some_and(|s| {
                        s.config
                            .workloads
                            .values()
                            .any(|w| w.unit == unit && w.scope == scope)
                    });
                let label = unit.clone();
                rows = rows.child(
                    h_flex()
                        .gap_2()
                        .child(
                            Button::new(("import", i))
                                .label(if registered { "Added" } else { "Import" })
                                .disabled(registered || busy || disconnected)
                                .on_click(cx.listener(move |this, _, _, _| {
                                    this.pending_imports.insert(format!("{scope}:{unit}"));
                                    this.send(Request::Import {
                                        unit: unit.clone(),
                                        scope,
                                    });
                                })),
                        )
                        .child(label),
                );
            }
            content = content.child(
                div()
                    .id("scan-list")
                    .h_full()
                    .flex_shrink_0()
                    .w(px(420.))
                    .overflow_y_scroll()
                    .bg(rgb(0x202936))
                    .p_3()
                    .child(rows),
            );
        }
        let selected_workload = self.selected.as_ref().filter(|s| !s.1).map(|s| s.0.clone());
        let mut journal_header = h_flex().gap_2().child("Service journal · latest 150 lines");
        if let Some(workload) = selected_workload {
            journal_header = journal_header.child(
                Button::new("refresh-logs")
                    .label("Refresh")
                    .disabled(disconnected)
                    .on_click(cx.listener(move |this, _, _, _| {
                        this.send(Request::Logs {
                            workload: workload.clone(),
                        })
                    })),
            );
        }
        let alert = self.snapshot.as_ref().and_then(|s| {
            s.monitoring
                .iter()
                .find(|(scope, health)| {
                    !health.available
                        && s.config.workloads.values().any(|w| {
                            w.backend == devdash::model::BackendKind::Systemd && w.scope == **scope
                        })
                })
                .map(|(scope, health)| {
                    format!(
                        "{scope} monitoring unavailable: {}",
                        health.reason.as_deref().unwrap_or("unknown reason")
                    )
                })
                .or_else(|| s.monitor_gap.clone())
                .or_else(|| {
                    s.run
                        .progress
                        .iter()
                        .rev()
                        .find_map(|p| p.error.clone())
                        .or_else(|| {
                            s.events
                                .iter()
                                .rev()
                                .find(|e| e.level == "warning")
                                .map(|e| {
                                    format!(
                                        "Latest warning: {} {}",
                                        e.workload.as_deref().unwrap_or("manager"),
                                        e.message
                                    )
                                })
                        })
                })
        });
        v_flex()
            .size_full()
            .p_4()
            .gap_3()
            .bg(rgb(0x10151d))
            .text_color(rgb(0xd6dce8))
            .text_sm()
            .child(toolbar)
            .child(
                div().text_color(rgb(0x9baac1)).child(
                    self.connection_error
                        .clone()
                        .unwrap_or_else(|| self.message.clone()),
                ),
            )
            .children(alert.map(|message| {
                div()
                    .max_h(px(70.))
                    .overflow_hidden()
                    .text_color(rgb(0xe6c07b))
                    .child(message)
            }))
            .child(content)
            .child(journal_header)
            .child(
                div()
                    .id("journal")
                    .h(px(160.))
                    .overflow_y_scroll()
                    .p_3()
                    .bg(rgb(0x080d14))
                    .rounded_md()
                    .child(self.logs.clone()),
            )
    }
}
fn main() {
    let mut args = std::env::args().skip(1);
    let socket = match args.next().as_deref() {
        Some("--socket") => PathBuf::from(args.next().expect("--socket requires a path")),
        None => ipc::socket_path(),
        _ => {
            eprintln!("Usage: devdash-gui [--socket PATH]");
            std::process::exit(2)
        }
    };
    gpui_kit::application().run(move |cx| {
        gpui_kit::init(cx);
        Theme::change(ThemeMode::Dark, None, cx);
        cx.on_window_closed(|cx, _| {
            if cx.windows().is_empty() {
                cx.quit();
            }
        })
        .detach();
        gpui_kit::open_window(
            WindowOptions {
                window_bounds: Some(WindowBounds::Windowed(Bounds::centered(
                    None,
                    size(px(1280.), px(820.)),
                    cx,
                ))),
                ..Default::default()
            },
            cx,
            |window, cx| {
                window.set_window_title("devdash");
                cx.new(|cx| Dashboard::new(socket, cx))
            },
        )
        .expect("open devdash window");
        cx.activate(true);
    });
}
