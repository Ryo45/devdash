//! Read-only dependency diagnostics. This graph never schedules service operations.
use crate::{engine::Action, model::*};
use anyhow::Result;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet, VecDeque};

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Relation {
    Wants,
    Requires,
    BindsTo,
    PartOf,
    After,
    Before,
    Conflicts,
    Requisite,
    Upholds,
    PropagatesStopTo,
}
impl Relation {
    pub fn starts(self) -> bool {
        matches!(
            self,
            Self::Wants | Self::Requires | Self::BindsTo | Self::Upholds
        )
    }
    fn hard_start(self) -> bool {
        matches!(self, Self::Requires | Self::BindsTo)
    }
    fn reverse_stop(self) -> bool {
        matches!(
            self,
            Self::Requires | Self::BindsTo | Self::PartOf | Self::Requisite
        )
    }
}
#[derive(Clone, Copy)]
enum Propagation {
    Start,
    StrongStart,
    Stop,
}
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct Edge {
    pub from: UnitKey,
    pub to: UnitKey,
    pub relation: Relation,
}
#[derive(Clone, Debug, Default)]
pub struct DependencyGraph {
    pub edges: BTreeSet<Edge>,
    pub inspected: BTreeSet<UnitKey>,
    pub aliases: BTreeMap<UnitKey, UnitKey>,
    pub incomplete: Vec<String>,
    pub stale: bool,
}
impl DependencyGraph {
    pub fn canonical<'a>(&'a self, key: &'a UnitKey) -> &'a UnitKey {
        self.aliases.get(key).unwrap_or(key)
    }
    pub fn edge(&mut self, from: UnitKey, to: UnitKey, relation: Relation) {
        self.edges.insert(Edge { from, to, relation });
    }
    fn reachable(&self, root: &UnitKey, mode: Propagation) -> BTreeMap<UnitKey, Vec<Edge>> {
        let root = self.canonical(root).clone();
        let mut paths = BTreeMap::from([(root.clone(), Vec::new())]);
        let mut pending = VecDeque::from([root]);
        while let Some(current) = pending.pop_front() {
            for edge in &self.edges {
                let from = self.canonical(&edge.from);
                let to = self.canonical(&edge.to);
                let next = if matches!(mode, Propagation::Stop) {
                    if edge.relation.reverse_stop() && to == &current {
                        Some(from)
                    } else if edge.relation == Relation::PropagatesStopTo && from == &current {
                        Some(to)
                    } else {
                        None
                    }
                } else if from == &current
                    && if matches!(mode, Propagation::StrongStart) {
                        edge.relation.hard_start()
                    } else {
                        edge.relation.starts()
                    }
                {
                    Some(to)
                } else {
                    None
                };
                if let Some(next) = next
                    && !paths.contains_key(next)
                {
                    let mut path = paths[&current].clone();
                    path.push(edge.clone());
                    paths.insert(next.clone(), path);
                    pending.push_back(next.clone());
                }
            }
        }
        paths
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Diagnostic {
    pub level: String,
    pub code: String,
    pub message: String,
    pub units: Vec<UnitKey>,
    pub path: Vec<String>,
}
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct InspectionReport {
    pub action: Option<Action>,
    pub stale: bool,
    pub diagnostics: Vec<Diagnostic>,
    pub inspected_units: usize,
    pub requested_units: Vec<UnitKey>,
    pub uninspected_units: Vec<UnitKey>,
    pub incomplete: Vec<String>,
}
impl InspectionReport {
    pub fn blocked(&self) -> bool {
        self.diagnostics.iter().any(|d| d.level == "error")
    }
    fn push(
        &mut self,
        level: &str,
        code: &str,
        message: String,
        units: Vec<UnitKey>,
        path: &[Edge],
    ) {
        self.diagnostics.push(Diagnostic {
            level: level.into(),
            code: code.into(),
            message,
            units,
            path: path
                .iter()
                .map(|e| format!("{} --{:?}--> {}", e.from, e.relation, e.to))
                .collect(),
        });
    }
}
fn placements(c: &Config, group: &str) -> Result<(Vec<Step>, BTreeMap<UnitKey, usize>)> {
    let steps = c.expand(group, false)?;
    let mut places = BTreeMap::new();
    for (i, step) in steps.iter().enumerate() {
        match step {
            Step::Workload(id) => {
                if let Some(key) = c.workloads[id].systemd_key() {
                    places.insert(key, i);
                }
            }
            Step::Parallel(ids) => {
                for id in ids {
                    if let Some(key) = c.workloads[id].systemd_key() {
                        places.insert(key, i);
                    }
                }
            }
            _ => {}
        }
    }
    Ok((steps, places))
}
pub fn inspect(
    c: &Config,
    group: &str,
    action: Action,
    approved: &BTreeSet<String>,
    graph: &DependencyGraph,
) -> Result<InspectionReport> {
    let (steps, raw) = placements(c, group)?;
    let places: BTreeMap<_, _> = raw
        .iter()
        .map(|(u, i)| (graph.canonical(u).clone(), *i))
        .collect();
    let mut report = InspectionReport {
        action: Some(action),
        stale: graph.stale,
        requested_units: raw.keys().cloned().collect(),
        uninspected_units: raw
            .keys()
            .filter(|u| !graph.inspected.contains(graph.canonical(u)))
            .cloned()
            .collect(),
        inspected_units: graph.inspected.len(),
        incomplete: graph.incomplete.clone(),
        ..Default::default()
    };
    if steps.iter().any(|step| match step {
        Step::Workload(id) => c.workloads[id].backend == BackendKind::Docker,
        Step::Parallel(ids) => ids
            .iter()
            .any(|id| c.workloads[id].backend == BackendKind::Docker),
        _ => false,
    }) {
        report.incomplete.push("Docker lifecycle dependencies are not inspected; internal controller children are outside Group/Steps".into());
    }
    if !report.uninspected_units.is_empty() {
        report.incomplete.push(format!(
            "Selected units not fully inspected: {}",
            report
                .uninspected_units
                .iter()
                .map(ToString::to_string)
                .collect::<Vec<_>>()
                .join(", ")
        ));
    }
    if raw.len() != places.len() {
        report.push(
            "error",
            "duplicate_alias",
            "Multiple Workloads in this group resolve to the same systemd unit".into(),
            raw.keys().cloned().collect(),
            &[],
        );
    }
    if action != Action::Stop {
        for (source, position) in &places {
            let hard = graph.reachable(source, Propagation::StrongStart);
            for (target, path) in graph.reachable(source, Propagation::Start) {
                if !places.contains_key(&target) {
                    let registered = c
                        .workloads
                        .iter()
                        .find(|(_, w)| {
                            w.backend == BackendKind::Systemd
                                && graph.canonical(&UnitKey::from(*w)) == &target
                        })
                        .map(|(id, _)| id);
                    let gates: Vec<_> = registered
                        .map(|id| {
                            c.manual_followers()
                                .into_iter()
                                .filter(|(_, w)| w == id)
                                .filter(|(g, _)| !approved.contains(g))
                                .map(|(g, _)| g)
                                .collect()
                        })
                        .unwrap_or_default();
                    let context = if gates.is_empty() {
                        String::new()
                    } else {
                        format!(
                            "; unapproved Manual Gates in the surrounding flow: {}",
                            gates.join(", ")
                        )
                    };
                    report.push("warning","outside_start",format!("Starting {source} may request {target} outside the selected group{context}"),vec![source.clone(),target.clone()],&path);
                }
                if let Some(later) = places.get(&target)
                    && later > position
                {
                    for step in &steps[position + 1..=*later] {
                        if let Step::Gate(gate) = step {
                            let manual = matches!(c.gates[gate], Gate::Manual);
                            if manual && action != Action::Reinitialize && approved.contains(gate) {
                                continue;
                            }
                            let level = if hard.contains_key(&target) {
                                "error"
                            } else {
                                "warning"
                            };
                            report.push(level,"gate_bypass",format!("Starting {source} requests {target} before gate {gate}; conditions and job success are not predicted"),vec![source.clone(),target.clone()],hard.get(&target).unwrap_or(&path));
                        }
                    }
                }
            }
        }
        for edge in &graph.edges {
            let from = graph.canonical(&edge.from);
            let to = graph.canonical(&edge.to);
            if edge.relation == Relation::Conflicts
                && places.contains_key(from)
                && places.contains_key(to)
            {
                report.push(
                    "error",
                    "conflicts",
                    format!("{from} and {to} cannot both remain active (Conflicts)"),
                    vec![from.clone(), to.clone()],
                    std::slice::from_ref(edge),
                );
            }
            if let (Some(a), Some(b)) = (places.get(from), places.get(to)) {
                let inverted = match edge.relation {
                    Relation::After => a < b,
                    Relation::Before => a > b,
                    _ => false,
                };
                if inverted {
                    report.push("warning","order_mismatch",format!("Steps order differs from {:?}: {} → {}. Ordering applies only to queued jobs; it does not start another unit",edge.relation,from,to),vec![from.clone(),to.clone()],std::slice::from_ref(edge));
                }
                if edge.relation == Relation::Requisite && a < b {
                    report.push(
                        "warning",
                        "requisite",
                        format!("{from} needs {to} already active; Requisite does not start it"),
                        vec![from.clone(), to.clone()],
                        std::slice::from_ref(edge),
                    );
                }
            }
        }
        for source in places.keys() {
            let started = graph.reachable(source, Propagation::Start);
            for edge in &graph.edges {
                if edge.relation == Relation::Conflicts {
                    let a = graph.canonical(&edge.from);
                    let b = graph.canonical(&edge.to);
                    let outside = if started.contains_key(a) && !places.contains_key(b) {
                        Some(b)
                    } else if started.contains_key(b) && !places.contains_key(a) {
                        Some(a)
                    } else {
                        None
                    };
                    if let Some(outside) = outside {
                        report.push(
                            "warning",
                            "outside_conflict",
                            format!("Starting {source} may stop {outside} through Conflicts"),
                            vec![source.clone(), outside.clone()],
                            std::slice::from_ref(edge),
                        );
                    }
                }
            }
        }
    }
    if action != Action::Start {
        for source in places.keys() {
            for (outside, path) in graph.reachable(source, Propagation::Stop) {
                if !places.contains_key(&outside) {
                    report.push("warning","outside_stop",format!("Stopping {source} can propagate to {outside} outside the selected group"),vec![source.clone(),outside],&path);
                }
            }
        }
    }
    for edge in &graph.edges {
        if edge.relation == Relation::Upholds
            && (places.contains_key(graph.canonical(&edge.from))
                || places.contains_key(graph.canonical(&edge.to)))
        {
            report.push(
                "warning",
                "upholds",
                format!(
                    "{} continuously upholds {}; explicit stop may be undone by systemd",
                    edge.from, edge.to
                ),
                vec![edge.from.clone(), edge.to.clone()],
                std::slice::from_ref(edge),
            );
        }
    }
    // Inverse D-Bus properties may describe the same relation twice.
    report
        .diagnostics
        .sort_by(|a, b| (&a.code, &a.message).cmp(&(&b.code, &b.message)));
    report
        .diagnostics
        .dedup_by(|a, b| a.code == b.code && a.message == b.message);
    Ok(report)
}
