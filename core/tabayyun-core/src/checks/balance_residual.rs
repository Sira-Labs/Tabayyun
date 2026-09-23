//! `tby.balance_residual` — an energy or mass balance does not close (catalogue #24, spec 011).
//!
//! A `balance` group has inputs and outputs. Per aligned bin (spec 008) where every member has
//! a value, the residual r = Σin − Σout is compared with the expected loss band
//! [`loss_min` · Σin, `loss_max` · Σin]. The bin is flagged when r lies outside the band by more
//! than k × σ_r, with σ_r = sqrt(Σ σ_i²) and σ_i = max(u_i · |x_i|, resolution_i). A loss inside
//! the band is never a finding, however precise the meters, and a crossing the meters cannot
//! resolve is not one either (spec 011, implementation edits). Runs shorter than `min_duration`
//! are dropped and the rest merge into episodes (`cross::episodes`).
//!
//! One balance equation cannot say which meter is off (every member's gross-error test equals
//! |r| / σ_r), so the suspect comes from change over time. Per bin, throughput
//! T = (Σin + Σout) / 2 and member share q_i = x_i / T. Scaling one meter also moves T and so
//! every other share by a common factor; that common mode is removed with the median relative
//! change g_m of all shares between the unflagged baseline B and the episode E. Member
//! contributions c_i = s_i · mean_B(q_i) · (g_i − g_m) (s_i = +1 for inputs, −1 for outputs)
//! sum to the episode's change in residual share net of the common mode; the member with
//! c_i / Σc ≥ 0.8 is the suspect.

use super::{duration_param, CheckContext, CheckOutput};
use crate::align::align;
use crate::cross::{
    episodes, member_name as name, num, with_group_params, CrossCheck, GroupKind, MemberRole, SeriesGroup,
};
use crate::error::{Error, Result};
use crate::finding::{Dimension, Finding, Metric, Severity};
use crate::frame::SeriesFrame;
use crate::profile::resolution;
use crate::time::{format_duration, NS_PER_DAY};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

pub const ID: &str = "tby.balance_residual";
/// Share of the episode's residual change one member must explain to be named.
const SUSPECT_SHARE: f64 = 0.8;
/// Below this net change in residual share nothing can be attributed.
const MIN_DELTA: f64 = 1e-9;
/// Relative standard uncertainty of a member the `uncertainty` map does not name.
const DEFAULT_UNCERTAINTY: f64 = 0.01;

/// Relative standard uncertainty: one value for every member, or per member series id
/// (members the map does not name use 0.01).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Uncertainty {
    All(f64),
    PerMember(BTreeMap<String, f64>),
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct BalanceResidual {
    /// Flag when the residual lies outside the loss band by more than k × σ_r.
    pub k: f64,
    pub uncertainty: Uncertainty,
    /// Expected loss share r / Σin: lower and upper bound.
    pub loss_min: f64,
    pub loss_max: f64,
    /// A violation must last this long; shorter gaps between violations merge.
    pub min_duration: String,
    /// Alignment grid: `auto` (coarsest expected interval) or a duration.
    pub grid: String,
    pub severity: Severity,
}

impl Default for BalanceResidual {
    fn default() -> Self {
        Self {
            k: 3.0,
            uncertainty: Uncertainty::All(DEFAULT_UNCERTAINTY),
            loss_min: -0.01,
            loss_max: 0.05,
            min_duration: "1h".into(),
            grid: "auto".into(),
            severity: Severity::High,
        }
    }
}

/// One complete bin (every member present).
struct Bin {
    ts: i64,
    x: Vec<f64>,
    residual: f64,
    /// r / Σin; NaN when Σin ≤ 0.
    share: f64,
    /// (Σin + Σout) / 2.
    throughput: f64,
    /// Distance outside the loss band in units of σ_r (0 inside the band).
    z: f64,
    flagged: bool,
    above: bool,
}

fn mean(xs: impl Iterator<Item = f64>) -> Option<f64> {
    let (sum, n) = xs.fold((0.0, 0usize), |(s, n), x| (s + x, n + 1));
    (n > 0).then(|| sum / n as f64)
}

/// The median, averaging the middle pair for an even count (`profile::median_mad` takes the
/// nearest rank, which would pin the common mode of a two-member balance on one member).
fn median(xs: &[f64]) -> Option<f64> {
    let mut v: Vec<f64> = xs.iter().copied().filter(|x| x.is_finite()).collect();
    if v.is_empty() {
        return None;
    }
    v.sort_by(f64::total_cmp);
    let n = v.len();
    Some(if n % 2 == 1 { v[n / 2] } else { (v[n / 2 - 1] + v[n / 2]) / 2.0 })
}

fn round3(x: f64) -> f64 {
    (x * 1000.0).round() / 1000.0
}

impl BalanceResidual {
    /// Per-member relative uncertainty in declaration order, after validation.
    fn member_uncertainty(&self, group: &SeriesGroup) -> std::result::Result<Vec<f64>, String> {
        let valid = |u: f64| u.is_finite() && u >= 0.0;
        match &self.uncertainty {
            Uncertainty::All(u) if valid(*u) => Ok(vec![*u; group.members.len()]),
            Uncertainty::All(_) => Err("uncertainty must be a non-negative number".into()),
            Uncertainty::PerMember(map) => {
                if let Some(id) = map.keys().find(|id| !group.member_ids().any(|m| m == id.as_str())) {
                    return Err(format!("uncertainty names {id}, which is not a member"));
                }
                if map.values().any(|u| !valid(*u)) {
                    return Err("uncertainty values must be non-negative numbers".into());
                }
                Ok(group.member_ids().map(|id| map.get(id).copied().unwrap_or(DEFAULT_UNCERTAINTY)).collect())
            }
        }
    }
}

impl CrossCheck for BalanceResidual {
    fn id(&self) -> &'static str {
        ID
    }
    fn dimension(&self) -> Dimension {
        Dimension::Consistency
    }
    fn default_severity(&self) -> Severity {
        self.severity
    }
    fn kinds(&self) -> &'static [GroupKind] {
        &[GroupKind::Balance]
    }

    fn run(&self, frames: &[&SeriesFrame], group: &SeriesGroup, ctx: &CheckContext) -> Result<CheckOutput> {
        let p = with_group_params(self, ID, group)?;
        let invalid = |reason: String| Error::InvalidParams {
            check: ID.into(),
            reason: format!("group {}: {reason}", group.id),
        };
        if !(p.k.is_finite() && p.k > 0.0) {
            return Err(invalid("k must be a positive number".into()));
        }
        if !(p.loss_min.is_finite() && p.loss_max.is_finite() && p.loss_min < p.loss_max) {
            return Err(invalid("loss_min must be below loss_max".into()));
        }
        let u = p.member_uncertainty(group).map_err(invalid)?;
        let min_duration = duration_param(ID, "min_duration", &p.min_duration)?;
        let grid = if p.grid == "auto" { None } else { Some(duration_param(ID, "grid", &p.grid)?) };
        if grid.is_some_and(|g| g <= 0) {
            return Err(invalid("grid must be positive".into()));
        }

        let mut out = CheckOutput::default();
        let m = frames.len();
        let sign: Vec<f64> =
            group.members.iter().map(|g| if g.role == MemberRole::Input { 1.0 } else { -1.0 }).collect();
        let first_input = sign.iter().position(|&s| s > 0.0).unwrap_or(0);
        let res: Vec<f64> = frames
            .iter()
            .map(|f| f.meta.resolution.or_else(|| resolution(&f.values)).unwrap_or(0.0))
            .collect();
        let aligned = align(frames, grid);
        if aligned.is_empty() || m < 2 || sign.len() != m {
            return Ok(out);
        }

        let mut bins: Vec<Bin> = Vec::with_capacity(aligned.len());
        for i in 0..aligned.len() {
            let x: Vec<f64> = aligned.columns.iter().map(|c| c[i]).collect();
            if x.iter().any(|v| !v.is_finite()) {
                continue; // a balance with a member missing is not a balance
            }
            let sum_in: f64 = (0..m).filter(|&j| sign[j] > 0.0).map(|j| x[j]).sum();
            let sum_out: f64 = (0..m).filter(|&j| sign[j] < 0.0).map(|j| x[j]).sum();
            let residual = sum_in - sum_out;
            let sigma = (0..m).map(|j| (u[j] * x[j].abs()).max(res[j]).powi(2)).sum::<f64>().sqrt();
            // Without input there is no share, and the band collapses to r = 0.
            let (lo, hi, share) = if sum_in > 0.0 {
                (p.loss_min * sum_in, p.loss_max * sum_in, residual / sum_in)
            } else {
                (0.0, 0.0, f64::NAN)
            };
            let excess = if residual > hi {
                residual - hi
            } else if residual < lo {
                lo - residual
            } else {
                0.0
            };
            let flagged = excess > p.k * sigma;
            let z = if sigma > 0.0 {
                excess / sigma
            } else if excess > 0.0 {
                f64::INFINITY
            } else {
                0.0
            };
            bins.push(Bin {
                ts: aligned.ts[i],
                throughput: (sum_in + sum_out) / 2.0,
                x,
                residual,
                share,
                z,
                flagged,
                above: residual > hi,
            });
        }

        let target_ids: Vec<&str> = frames.iter().map(|f| f.meta.id.as_str()).collect();
        self.metrics(&bins, target_ids[first_input], &group.id, &mut out);

        let ts: Vec<i64> = bins.iter().map(|b| b.ts).collect();
        let flags: Vec<bool> = bins.iter().map(|b| b.flagged).collect();
        let unit = frames[0].meta.unit.as_deref().map(|u| format!(" {u}")).unwrap_or_default();
        let baseline: Vec<&Bin> = bins.iter().filter(|b| !b.flagged && b.throughput > 0.0).collect();
        for ep in episodes(&ts, &flags, aligned.grid_ns, min_duration) {
            let ep_bins: Vec<&Bin> = ep.bins.iter().map(|&k| &bins[k]).collect();
            let above = ep_bins.iter().filter(|b| b.above).count() * 2 >= ep_bins.len();
            let (suspect, contributions) = attribute(&baseline, &ep_bins, &sign);
            let residual_mean = mean(ep_bins.iter().map(|b| b.residual)).unwrap_or(0.0);
            let share_mean = mean(ep_bins.iter().map(|b| b.share).filter(|s| s.is_finite()));
            let z_max = ep_bins.iter().map(|b| b.z).fold(0.0_f64, f64::max);

            let dur = format_duration(ep.window.duration());
            let gap = match share_mean {
                Some(s) if s < 0.0 => format!("outputs exceed inputs by {} %", num(-s * 100.0)),
                Some(s) => format!("inputs exceed outputs by {} %", num(s * 100.0)),
                None => format!("the residual is {}{unit} with no input", num(residual_mean)),
            };
            let blame = match suspect {
                Some(j) => format!("{} explains most of it", name(frames[j])),
                None => "no single member explains it".into(),
            };
            let summary = format!(
                "Balance {} does not close for {dur}: {gap} (band {} % to {} %); {blame}",
                group.name,
                num(p.loss_min * 100.0),
                num(p.loss_max * 100.0),
            );
            let ids = |side: f64| -> Vec<&str> {
                (0..m).filter(|&j| sign[j] == side).map(|j| target_ids[j]).collect()
            };
            let mut evidence = group.evidence_base();
            evidence.extend(
                serde_json::json!({
                    "inputs": ids(1.0), "outputs": ids(-1.0),
                    "suspect": suspect.map(|j| target_ids[j]),
                    "residual_mean": residual_mean, "z_max": z_max,
                    "loss_share_mean": share_mean,
                    "loss_min": p.loss_min, "loss_max": p.loss_max,
                    "contributions": contributions.map(|c| (0..m).map(|j| (target_ids[j].to_string(), round3(c[j]))).collect::<BTreeMap<_, _>>()),
                    "reason": if above { "above_band" } else { "below_band" },
                    "duration_ns": ep.window.duration(), "n_points": ep_bins.len(),
                })
                .as_object()
                .cloned()
                .unwrap_or_default(),
            );
            let fraction = ep.window.duration() as f64 / ctx.window.duration().max(1) as f64;
            out.findings.push(Finding::new(
                ID,
                target_ids[suspect.unwrap_or(first_input)],
                Dimension::Consistency,
                p.severity,
                ep.window,
                fraction,
                summary,
                serde_json::Value::Object(evidence),
            ));
        }
        Ok(out)
    }
}

/// The suspect and every member's share of the episode's residual change, net of the common
/// mode (see the module docs). `None` for the contributions when the baseline or the episode
/// has no bin with throughput, a member's baseline share is zero, or nothing changed.
fn attribute(baseline: &[&Bin], episode: &[&Bin], sign: &[f64]) -> (Option<usize>, Option<Vec<f64>>) {
    let m = sign.len();
    let shares = |bins: &[&Bin], j: usize| {
        mean(bins.iter().filter(|b| b.throughput > 0.0).map(|b| b.x[j] / b.throughput))
    };
    let (Some(mb), Some(me)) = (
        (0..m).map(|j| shares(baseline, j)).collect::<Option<Vec<f64>>>(),
        (0..m).map(|j| shares(episode, j)).collect::<Option<Vec<f64>>>(),
    ) else {
        return (None, None);
    };
    if mb.iter().any(|q| q.abs() < 1e-12) {
        return (None, None); // a member idle in the baseline has no relative change
    }
    let g: Vec<f64> = (0..m).map(|j| me[j] / mb[j] - 1.0).collect();
    let Some(g_common) = median(&g) else { return (None, None) };
    let c: Vec<f64> = (0..m).map(|j| sign[j] * mb[j] * (g[j] - g_common)).collect();
    let delta: f64 = c.iter().sum();
    if delta.abs() < MIN_DELTA {
        return (None, None);
    }
    let ratios: Vec<f64> = c.iter().map(|ci| ci / delta).collect();
    let suspect =
        (0..m).filter(|&j| ratios[j] >= SUSPECT_SHARE).max_by(|&a, &b| ratios[a].total_cmp(&ratios[b]));
    (suspect, Some(ratios))
}

impl BalanceResidual {
    /// Per UTC day, the mean residual and mean loss share, on the first input and named after
    /// the group so a series in two balances keeps both lines.
    fn metrics(&self, bins: &[Bin], series_id: &str, group_key: &str, out: &mut CheckOutput) {
        let mut days: BTreeMap<i64, (Vec<f64>, Vec<f64>)> = BTreeMap::new();
        for b in bins {
            let day = days.entry(b.ts.div_euclid(NS_PER_DAY) * NS_PER_DAY).or_default();
            day.0.push(b.residual);
            if b.share.is_finite() {
                day.1.push(b.share);
            }
        }
        for (start, (residuals, shares)) in days {
            for (name, values) in [("residual_mean", residuals), ("loss_share_mean", shares)] {
                if let Some(v) = mean(values.into_iter()) {
                    out.metrics.push(Metric {
                        check_id: ID.into(),
                        series_id: series_id.into(),
                        name: format!("{name}:{group_key}"),
                        ts: start,
                        value: v,
                    });
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cross::GroupMember;
    use crate::frame::SeriesMeta;
    use crate::synth::Rng;
    use crate::time::NS_PER_MIN;

    const T0: i64 = 1_704_067_200 * 1_000_000_000; // 2024-01-01T00:00Z
    const STEP: i64 = 10 * NS_PER_MIN;
    const N: usize = 3 * 144; // three days of 10-minute bins
    /// Bins 200..236: six hours on the second day.
    const EP: std::ops::Range<usize> = 200..236;

    /// A meter reading `base` × a daily load curve × `factor(i)`, with 0.2 % noise.
    fn meter(id: &str, seed: u64, base: f64, factor: impl Fn(usize) -> f64) -> SeriesFrame {
        let mut rng = Rng::new(seed);
        let values = (0..N)
            .map(|i| {
                let load = 1.0 + 0.2 * (i as f64 / 144.0 * std::f64::consts::TAU).sin();
                base * load * factor(i) * (1.0 + 0.002 * rng.normal())
            })
            .collect();
        let ts = (0..N as i64).map(|i| T0 + i * STEP).collect();
        let mut meta = SeriesMeta::new(id);
        meta.name = Some(id.to_uppercase());
        meta.unit = Some("MW".into());
        SeriesFrame::with_default_quality(meta, ts, values).unwrap()
    }

    fn during(range: std::ops::Range<usize>, by: f64) -> impl Fn(usize) -> f64 {
        move |i| if range.contains(&i) { by } else { 1.0 }
    }

    fn group(ins: &[&SeriesFrame], outs: &[&SeriesFrame], params: serde_json::Value) -> SeriesGroup {
        let member = |f: &&SeriesFrame, role| GroupMember { series_id: f.meta.id.clone(), role };
        SeriesGroup {
            id: "g-ss".into(),
            name: "SS-North".into(),
            kind: GroupKind::Balance,
            members: ins
                .iter()
                .map(|f| member(f, MemberRole::Input))
                .chain(outs.iter().map(|f| member(f, MemberRole::Output)))
                .collect(),
            params,
        }
    }

    fn check(ins: &[&SeriesFrame], outs: &[&SeriesFrame], params: serde_json::Value) -> Result<CheckOutput> {
        let frames: Vec<&SeriesFrame> = ins.iter().chain(outs).copied().collect();
        let ctx = CheckContext::from_frame(frames[0]);
        BalanceResidual::default().run(&frames, &group(ins, outs, params), &ctx)
    }

    fn run(ins: &[&SeriesFrame], outs: &[&SeriesFrame], params: serde_json::Value) -> CheckOutput {
        check(ins, outs, params).unwrap()
    }

    /// One input of 100 and three feeders of 40, 30 and 27: a 3 % loss.
    fn substation(
        feeder: impl Fn(usize) -> Box<dyn Fn(usize) -> f64>,
    ) -> (SeriesFrame, SeriesFrame, SeriesFrame, SeriesFrame) {
        (
            meter("in", 1, 100.0, |_| 1.0),
            meter("f1", 2, 40.0, feeder(0)),
            meter("f2", 3, 30.0, feeder(1)),
            meter("f3", 4, 27.0, feeder(2)),
        )
    }

    #[test]
    fn closing_balance_is_silent() {
        let (i, a, b, c) = substation(|_| Box::new(|_| 1.0));
        let out = run(&[&i], &[&a, &b, &c], serde_json::Value::Null);
        assert!(out.findings.is_empty(), "{:?}", out.findings);
        // Three days of metrics: residual and loss share per day, on the input.
        assert_eq!(out.metrics.len(), 6);
        assert!(out.metrics.iter().all(|m| m.series_id == "in"));
        let share = out.metrics.iter().find(|m| m.name == "loss_share_mean:g-ss").unwrap().value;
        assert!((share - 0.03).abs() < 0.002, "{share}");
    }

    #[test]
    fn loss_inside_band_is_silent_even_with_precise_meters() {
        // A 4.5 % loss is 3.3 σ_r with 1 % meters: the spec's first rule flagged it.
        let i = meter("in", 1, 100.0, |_| 1.0);
        let o = meter("out", 2, 95.5, |_| 1.0);
        for u in [0.01, 0.001] {
            let out = run(&[&i], &[&o], serde_json::json!({"uncertainty": u}));
            assert!(out.findings.is_empty(), "u {u}: {:?}", out.findings);
        }
    }

    #[test]
    fn scaled_outlet_named() {
        // Feeder f3 reads 30 % low for 6 h: the loss share goes from 3 % to 11 %.
        let (i, a, b, c) = substation(|j| Box::new(move |k| if j == 2 { during(EP, 0.7)(k) } else { 1.0 }));
        let out = run(&[&i], &[&a, &b, &c], serde_json::Value::Null);
        assert_eq!(out.findings.len(), 1, "{:?}", out.findings);
        let f = &out.findings[0];
        assert_eq!(f.series_id, "f3");
        assert_eq!(f.evidence["suspect"], "f3");
        assert_eq!(f.evidence["reason"], "above_band");
        assert_eq!(f.evidence["inputs"], serde_json::json!(["in"]));
        assert_eq!(f.evidence["outputs"], serde_json::json!(["f1", "f2", "f3"]));
        assert_eq!((f.window.start, f.window.end), (T0 + 200 * STEP, T0 + 236 * STEP));
        assert!(f.evidence["contributions"]["f3"].as_f64().unwrap() > 0.9, "{}", f.evidence);
        let share = f.evidence["loss_share_mean"].as_f64().unwrap();
        assert!((share - 0.111).abs() < 0.003, "{share}"); // 3 % + 8.1 of 100
        assert_eq!(
            f.summary,
            format!(
                "Balance SS-North does not close for 6h: inputs exceed outputs by {} % (band -1 % to 5 %); \
                 F3 explains most of it",
                num(share * 100.0)
            )
        );
    }

    #[test]
    fn small_scaling_needs_precise_meters_or_a_tight_band() {
        // f3 reading 10 % low moves the loss share from 3 % to 5.7 %: 0.7 % past a 5 % band,
        // below 3 σ_r with 1 % meters. Class 0.2 meters and a 4 % band resolve it.
        let (i, a, b, c) = substation(|j| Box::new(move |k| if j == 2 { during(EP, 0.9)(k) } else { 1.0 }));
        assert!(run(&[&i], &[&a, &b, &c], serde_json::Value::Null).findings.is_empty());
        let out = run(&[&i], &[&a, &b, &c], serde_json::json!({"uncertainty": 0.002, "loss_max": 0.04}));
        assert_eq!(out.findings.len(), 1, "{:?}", out.findings);
        assert_eq!(out.findings[0].evidence["suspect"], "f3");
    }

    #[test]
    fn load_change_keeps_suspect() {
        // Every member reads 30 % more during the episode; f2 additionally reads 30 % low.
        let load = |k: usize| if EP.contains(&k) { 1.3 } else { 1.0 };
        let i = meter("in", 1, 100.0, load);
        let a = meter("f1", 2, 40.0, load);
        let b = meter("f2", 3, 30.0, move |k| load(k) * during(EP, 0.7)(k));
        let c = meter("f3", 4, 27.0, load);
        let out = run(&[&i], &[&a, &b, &c], serde_json::Value::Null);
        assert_eq!(out.findings.len(), 1, "{:?}", out.findings);
        assert_eq!(out.findings[0].evidence["suspect"], "f2");
    }

    #[test]
    fn inlet_and_outlet_alone_name_no_suspect() {
        let i = meter("in", 1, 100.0, |_| 1.0);
        let o = meter("out", 2, 97.0, during(EP, 0.9));
        let out = run(&[&i], &[&o], serde_json::Value::Null);
        assert_eq!(out.findings.len(), 1, "{:?}", out.findings);
        let f = &out.findings[0];
        assert!(f.evidence["suspect"].is_null(), "{}", f.evidence);
        assert_eq!(f.series_id, "in");
        assert!(f.summary.ends_with("no single member explains it"), "{}", f.summary);
    }

    #[test]
    fn joint_drift_no_suspect() {
        let (i, a, b, c) = substation(|j| Box::new(move |k| if j < 2 { during(EP, 0.9)(k) } else { 1.0 }));
        let out = run(&[&i], &[&a, &b, &c], serde_json::Value::Null);
        assert_eq!(out.findings.len(), 1, "{:?}", out.findings);
        let f = &out.findings[0];
        assert!(f.evidence["suspect"].is_null(), "{}", f.evidence);
        // The blame is spread: no member explains 80 %, and both drifting feeders take part.
        let c = f.evidence["contributions"].as_object().unwrap();
        assert!(c.values().all(|v| v.as_f64().unwrap() < SUSPECT_SHARE), "{c:?}");
        assert!(c["f1"].as_f64().unwrap() > 0.1 && c["f2"].as_f64().unwrap() > 0.1, "{c:?}");
    }

    #[test]
    fn loss_band_violation() {
        // An 8 % loss all window long with precise meters: every bin is flagged, so there is no
        // baseline to attribute against.
        let i = meter("in", 1, 100.0, |_| 1.0);
        let o = meter("out", 2, 92.0, |_| 1.0);
        let out = run(&[&i], &[&o], serde_json::json!({"uncertainty": 0.001}));
        assert_eq!(out.findings.len(), 1, "{:?}", out.findings);
        let f = &out.findings[0];
        assert_eq!(f.evidence["reason"], "above_band");
        assert!(f.evidence["suspect"].is_null() && f.evidence["contributions"].is_null());
        assert_eq!(f.window.duration(), N as i64 * STEP);
        assert!(f.evidence["z_max"].as_f64().unwrap() > 3.0);
        // A 6 % loss is 1 % outside the band, well within the uncertainty of 5 % meters.
        let o = meter("out", 2, 94.0, |_| 1.0);
        assert!(run(&[&i], &[&o], serde_json::json!({"uncertainty": 0.05})).findings.is_empty());
    }

    #[test]
    fn outputs_exceeding_inputs() {
        let (i, a, b, c) = substation(|j| Box::new(move |k| if j == 0 { during(EP, 1.2)(k) } else { 1.0 }));
        let out = run(&[&i], &[&a, &b, &c], serde_json::Value::Null);
        assert_eq!(out.findings.len(), 1, "{:?}", out.findings);
        let f = &out.findings[0];
        assert_eq!(f.evidence["reason"], "below_band");
        assert_eq!(f.evidence["suspect"], "f1");
        assert!(f.summary.contains(": outputs exceed inputs by "), "{}", f.summary);
    }

    #[test]
    fn missing_member_bins_ignored() {
        // f3 reads 30 % low only while f2 is missing: those bins are not a balance.
        let (i, a, mut b, c) =
            substation(|j| Box::new(move |k| if j == 2 { during(EP, 0.7)(k) } else { 1.0 }));
        for k in EP {
            b.values[k] = f64::NAN;
        }
        let out = run(&[&i], &[&a, &b, &c], serde_json::Value::Null);
        assert!(out.findings.is_empty(), "{:?}", out.findings);
    }

    #[test]
    fn per_member_uncertainty() {
        // A 10 % loss: flagged with 1 % meters, within the uncertainty once the outlet is a 5 %
        // meter; members the map leaves out keep 1 %.
        let i = meter("in", 1, 100.0, |_| 1.0);
        let o = meter("out", 2, 90.0, |_| 1.0);
        assert_eq!(run(&[&i], &[&o], serde_json::Value::Null).findings.len(), 1);
        let out = run(&[&i], &[&o], serde_json::json!({"uncertainty": {"out": 0.05}}));
        assert!(out.findings.is_empty(), "{:?}", out.findings);
    }

    #[test]
    fn idle_members_floor_uncertainty_at_resolution() {
        // An idle line: the input reads 0, the outlet a constant 0.01 (one count). Without the
        // resolution floor σ_r would be 1e-4 and the whole window would be flagged.
        let idle = |id: &str, v: f64| {
            let mut meta = SeriesMeta::new(id);
            meta.resolution = Some(0.01);
            SeriesFrame::with_default_quality(
                meta,
                (0..N as i64).map(|i| T0 + i * STEP).collect(),
                vec![v; N],
            )
            .unwrap()
        };
        let (i, o) = (idle("in", 0.0), idle("out", 0.01));
        assert!(run(&[&i], &[&o], serde_json::Value::Null).findings.is_empty());
    }

    #[test]
    fn bad_params_are_invalid() {
        let i = meter("in", 1, 100.0, |_| 1.0);
        let o = meter("out", 2, 97.0, |_| 1.0);
        for (params, message) in [
            (serde_json::json!({"k": 0.0}), "k must be a positive"),
            (serde_json::json!({"loss_min": 0.05, "loss_max": 0.01}), "loss_min must be below"),
            (serde_json::json!({"uncertainty": -0.1}), "non-negative number"),
            (serde_json::json!({"uncertainty": {"in": -1.0}}), "non-negative numbers"),
            (serde_json::json!({"uncertainty": {"f9": 0.02}}), "f9, which is not a member"),
            (serde_json::json!({"uncertainty": "high"}), "did not match"),
            (serde_json::json!({"grid": "0s"}), "grid must be positive"),
            (serde_json::json!({"min_duration": "soon"}), "cannot parse duration"),
        ] {
            let err = check(&[&i], &[&o], params).unwrap_err();
            assert!(matches!(err, Error::InvalidParams { .. }) && err.to_string().contains(message), "{err}");
        }
    }

    #[test]
    fn registry_runs_it_on_balance_groups_only() {
        use crate::registry::{CheckConfig, Registry};
        let frames = vec![meter("in", 1, 100.0, |_| 1.0), meter("out", 2, 97.0, during(EP, 0.9))];
        let ctx = CheckContext::from_frame(&frames[0]);
        let configs = vec![CheckConfig { id: ID.into(), params: serde_json::Value::Null, enabled: true }];
        let balance = group(&[&frames[0]], &[&frames[1]], serde_json::Value::Null);
        let out = Registry::run_multi(&configs, &frames, &BTreeMap::new(), &[balance], &ctx).unwrap();
        assert_eq!(out.per_series["in"].findings.len(), 1);
        let related = SeriesGroup {
            kind: GroupKind::Related,
            members: frames
                .iter()
                .map(|f| GroupMember { series_id: f.meta.id.clone(), role: MemberRole::Member })
                .collect(),
            ..group(&[&frames[0]], &[&frames[1]], serde_json::Value::Null)
        };
        let out = Registry::run_multi(&configs, &frames, &BTreeMap::new(), &[related], &ctx).unwrap();
        assert!(out.per_series["in"].findings.is_empty());
        assert!(Registry::default_multi_configs().iter().any(|c| c.id == ID));
    }
}
