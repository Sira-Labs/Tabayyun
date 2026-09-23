//! `tby.redundant_disagreement` — redundant sensors disagree (catalogue #23, spec 010).
//!
//! The members of a `redundant` group measure the same quantity. After alignment (spec 008)
//! a bin counts when at least two members have a value. With two members the difference
//! d = A − B is compared with the tolerance around zero, so a constant bias larger than the
//! noise is a disagreement. With three or more, each member is compared with the bin's
//! median, and a bin where exactly one member deviates names that member as the bin's
//! suspect. Runs of disagreeing bins shorter than `min_duration` are dropped, the rest merge
//! when they are closer than `min_duration`, and each episode becomes one finding, attached to
//! the member that was the suspect in at least 80 % of its bins (else to the first member,
//! `suspect` null).
//!
//! Tolerance: `tolerance` (engineering units), else `tolerance_pct` of the members' median
//! magnitude, else automatic: k × 1.4826 × MAD of the differences (pair) or of the deviations
//! from the bin median (three or more), floored at twice the coarsest member resolution.

use super::{duration_param, CheckContext, CheckOutput};
use crate::align::align;
use crate::cross::{
    episodes, member_name as name, num, with_group_params, CrossCheck, GroupKind, SeriesGroup,
};
use crate::error::{Error, Result};
use crate::finding::{Dimension, Finding, Metric, Severity};
use crate::frame::SeriesFrame;
use crate::profile::{median_mad, resolution};
use crate::time::{format_duration, NS_PER_DAY};
use serde::{Deserialize, Serialize};

pub const ID: &str = "tby.redundant_disagreement";
/// Share of an episode's bins in which one member must be the suspect to be named.
const SUSPECT_SHARE: f64 = 0.8;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct RedundantDisagreement {
    /// Allowed absolute difference, in the members' engineering unit.
    pub tolerance: Option<f64>,
    /// Allowed difference as a percentage of the members' median magnitude.
    pub tolerance_pct: Option<f64>,
    /// Automatic tolerance = k × 1.4826 × MAD of the differences.
    pub k: f64,
    /// A disagreement must last this long; shorter gaps between disagreements merge.
    pub min_duration: String,
    /// Alignment grid: `auto` (coarsest expected interval) or a duration.
    pub grid: String,
    pub severity: Severity,
}

impl Default for RedundantDisagreement {
    fn default() -> Self {
        Self {
            tolerance: None,
            tolerance_pct: None,
            k: 4.0,
            min_duration: "15m".into(),
            grid: "auto".into(),
            severity: Severity::High,
        }
    }
}

/// Per bin: each member's signed deviation (NaN when absent) and the bin's suspect.
struct Bin {
    ts: i64,
    deviations: Vec<f64>,
    disagrees: bool,
    suspect: Option<usize>,
}

fn median(xs: &[f64]) -> Option<f64> {
    median_mad(xs).map(|(m, _)| m)
}

impl RedundantDisagreement {
    /// The tolerance to judge with and where it came from (`param`, `pct` or `auto`).
    fn tolerance(&self, frames: &[&SeriesFrame], deviations: &[f64]) -> Result<(f64, &'static str)> {
        let invalid = |reason: &str| Error::InvalidParams { check: ID.into(), reason: reason.into() };
        if let Some(t) = self.tolerance {
            if !(t.is_finite() && t > 0.0) {
                return Err(invalid("tolerance must be a positive number"));
            }
            return Ok((t, "param"));
        }
        if let Some(pct) = self.tolerance_pct {
            if !(pct.is_finite() && pct > 0.0) {
                return Err(invalid("tolerance_pct must be a positive number"));
            }
            let magnitudes: Vec<f64> = frames
                .iter()
                .flat_map(|f| f.values.iter().filter(|v| v.is_finite()).map(|v| v.abs()))
                .collect();
            let scale = median(&magnitudes).unwrap_or(0.0);
            // Members that mostly read 0 (an idle line) have scale 0: floor like the auto case.
            return Ok(((pct / 100.0 * scale).max(resolution_floor(frames)).max(f64::MIN_POSITIVE), "pct"));
        }
        if !(self.k.is_finite() && self.k > 0.0) {
            return Err(invalid("k must be a positive number"));
        }
        let spread = median_mad(deviations).map_or(0.0, |(_, mad)| self.k * 1.4826 * mad);
        Ok((spread.max(resolution_floor(frames)).max(f64::MIN_POSITIVE), "auto"))
    }
}

/// Twice the coarsest member resolution (metadata, else estimated from the data): identical
/// quantised readings would otherwise give a zero tolerance, judged finer than the
/// instruments read.
fn resolution_floor(frames: &[&SeriesFrame]) -> f64 {
    frames.iter().filter_map(|f| f.meta.resolution.or_else(|| resolution(&f.values))).fold(0.0_f64, f64::max)
        * 2.0
}

impl CrossCheck for RedundantDisagreement {
    fn id(&self) -> &'static str {
        ID
    }
    fn dimension(&self) -> Dimension {
        Dimension::Accuracy
    }
    fn default_severity(&self) -> Severity {
        self.severity
    }
    fn kinds(&self) -> &'static [GroupKind] {
        &[GroupKind::Redundant]
    }

    fn run(&self, frames: &[&SeriesFrame], group: &SeriesGroup, ctx: &CheckContext) -> Result<CheckOutput> {
        let p = with_group_params(self, ID, group)?;
        let invalid = |reason: &str| Error::InvalidParams {
            check: ID.into(),
            reason: format!("group {}: {reason}", group.id),
        };
        let min_duration = duration_param(ID, "min_duration", &p.min_duration)?;
        let grid = if p.grid == "auto" { None } else { Some(duration_param(ID, "grid", &p.grid)?) };
        if grid.is_some_and(|g| g <= 0) {
            return Err(invalid("grid must be positive"));
        }
        let mut out = CheckOutput::default();
        let aligned = align(frames, grid);
        let m = frames.len();
        if aligned.is_empty() || m < 2 {
            return Ok(out);
        }

        // Deviations: for a pair, both members carry d = A − B (the pair's difference); for
        // three or more, each member's distance from the bin median.
        let mut bins: Vec<Bin> = Vec::with_capacity(aligned.len());
        for i in 0..aligned.len() {
            let values: Vec<f64> = aligned.columns.iter().map(|c| c[i]).collect();
            let present: Vec<f64> = values.iter().copied().filter(|v| v.is_finite()).collect();
            if present.len() < 2 {
                continue; // a single member cannot disagree with anyone
            }
            let deviations: Vec<f64> = if m == 2 {
                let d = values[0] - values[1];
                vec![d, d]
            } else {
                let med = median(&present).unwrap_or(f64::NAN);
                values.iter().map(|v| v - med).collect()
            };
            bins.push(Bin { ts: aligned.ts[i], deviations, disagrees: false, suspect: None });
        }
        let pooled: Vec<f64> = if m == 2 {
            bins.iter().map(|b| b.deviations[0]).collect()
        } else {
            bins.iter().flat_map(|b| b.deviations.iter().copied().filter(|d| d.is_finite())).collect()
        };
        let (tol, source) = p.tolerance(frames, &pooled)?;
        // A pair's auto tolerance is centred at zero, not at the typical difference, so a
        // constant bias larger than the noise is a disagreement.
        for b in &mut bins {
            let deviating: Vec<usize> = (0..m).filter(|&j| b.deviations[j].abs() > tol).collect();
            b.disagrees = !deviating.is_empty();
            let present = b.deviations.iter().filter(|d| d.is_finite()).count();
            if m >= 3 && present >= 3 && deviating.len() == 1 {
                b.suspect = Some(deviating[0]);
            }
        }

        let group_key = &group.id;
        self.metrics(frames, &bins, group_key, &mut out);

        let ts: Vec<i64> = bins.iter().map(|b| b.ts).collect();
        let flags: Vec<bool> = bins.iter().map(|b| b.disagrees).collect();

        let unit = frames[0].meta.unit.as_deref().map(|u| format!(" {u}")).unwrap_or_default();
        for ep in episodes(&ts, &flags, aligned.grid_ns, min_duration) {
            let (w, idx) = (ep.window, ep.bins);
            let mut counts = vec![0usize; m];
            for &k in &idx {
                if let Some(j) = bins[k].suspect {
                    counts[j] += 1;
                }
            }
            let suspect = (0..m).find(|&j| counts[j] as f64 >= SUSPECT_SHARE * idx.len() as f64);
            // The size of the disagreement: the suspect's distance, else the largest member's.
            let sizes: Vec<f64> = idx
                .iter()
                .map(|&k| match suspect {
                    Some(j) => bins[k].deviations[j].abs(),
                    None => bins[k]
                        .deviations
                        .iter()
                        .filter(|d| d.is_finite())
                        .fold(0.0_f64, |a, d| a.max(d.abs())),
                })
                .filter(|d| d.is_finite())
                .collect();
            let max_abs = sizes.iter().fold(0.0_f64, |a, &d| a.max(d));
            let mean_abs = sizes.iter().sum::<f64>() / sizes.len().max(1) as f64;
            let target = frames[suspect.unwrap_or(0)];
            let dur = format_duration(w.duration());
            let summary = match suspect {
                Some(j) => {
                    let others: Vec<&str> = (0..m).filter(|&o| o != j).map(|o| name(frames[o])).collect();
                    format!(
                        "{} reads up to {}{unit} away from {} for {dur} (tolerance {}{unit})",
                        name(frames[j]),
                        num(max_abs),
                        others.join("/"),
                        num(tol)
                    )
                }
                None if m == 2 => format!(
                    "{} and {} disagree by up to {}{unit} for {dur} (tolerance {}{unit}); cannot tell which is off",
                    name(frames[0]),
                    name(frames[1]),
                    num(max_abs),
                    num(tol)
                ),
                None => format!(
                    "{} disagree by up to {}{unit} for {dur} (tolerance {}{unit}); no single member stands out",
                    frames.iter().map(|f| name(f)).collect::<Vec<_>>().join("/"),
                    num(max_abs),
                    num(tol)
                ),
            };
            let mut evidence = group.evidence_base();
            evidence.extend(
                serde_json::json!({
                    "suspect": suspect.map(|j| frames[j].meta.id.clone()),
                    "tolerance": tol, "tolerance_source": source,
                    "max_abs_diff": max_abs, "mean_abs_diff": mean_abs,
                    "duration_ns": w.duration(), "n_points": idx.len(),
                })
                .as_object()
                .cloned()
                .unwrap_or_default(),
            );
            let fraction = w.duration() as f64 / ctx.window.duration().max(1) as f64;
            out.findings.push(Finding::new(
                ID,
                &target.meta.id,
                Dimension::Accuracy,
                p.severity,
                w,
                fraction,
                summary,
                serde_json::Value::Object(evidence),
            ));
        }
        Ok(out)
    }
}

impl RedundantDisagreement {
    /// Per member and UTC day, the largest |deviation|, named after the group so a series in
    /// two redundant groups keeps both lines.
    fn metrics(&self, frames: &[&SeriesFrame], bins: &[Bin], group_key: &str, out: &mut CheckOutput) {
        let name = format!("max_abs_diff:{group_key}");
        for (j, f) in frames.iter().enumerate() {
            let mut day: Option<(i64, f64)> = None;
            let flush = |d: Option<(i64, f64)>, out: &mut CheckOutput| {
                if let Some((start, v)) = d {
                    out.metrics.push(Metric {
                        check_id: ID.into(),
                        series_id: f.meta.id.clone(),
                        name: name.clone(),
                        ts: start,
                        value: v,
                    });
                }
            };
            for b in bins {
                let d = b.deviations[j];
                if !d.is_finite() {
                    continue;
                }
                let start = b.ts.div_euclid(NS_PER_DAY) * NS_PER_DAY;
                match day {
                    Some((s, v)) if s == start => day = Some((s, v.max(d.abs()))),
                    _ => {
                        flush(day, out);
                        day = Some((start, d.abs()));
                    }
                }
            }
            flush(day, out);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cross::{GroupMember, MemberRole};
    use crate::frame::SeriesMeta;
    use crate::synth::Rng;
    use crate::time::NS_PER_MIN;

    const T0: i64 = 1_704_067_200 * 1_000_000_000; // 2024-01-01T00:00Z
    const N: usize = 2 * 1440; // two days of minutes

    /// A shared process value plus independent sensor noise; `shift(i)` offsets the sensor.
    fn sensor(id: &str, seed: u64, scale: f64, shift: impl Fn(usize) -> f64) -> SeriesFrame {
        let mut rng = Rng::new(seed);
        let values = (0..N)
            .map(|i| {
                scale * (50.0 + 5.0 * (i as f64 / 1440.0 * std::f64::consts::TAU).sin())
                    + shift(i)
                    + 0.05 * rng.normal()
            })
            .collect();
        let ts = (0..N as i64).map(|i| T0 + i * NS_PER_MIN).collect();
        let mut meta = SeriesMeta::new(id);
        meta.name = Some(id.to_uppercase());
        meta.unit = Some("bar".into());
        SeriesFrame::with_default_quality(meta, ts, values).unwrap()
    }

    fn run(frames: &[&SeriesFrame], params: serde_json::Value) -> CheckOutput {
        let group = SeriesGroup {
            id: "g-pt".into(),
            name: "PT-101".into(),
            kind: GroupKind::Redundant,
            members: frames
                .iter()
                .map(|f| GroupMember { series_id: f.meta.id.clone(), role: MemberRole::Member })
                .collect(),
            params,
        };
        let ctx = CheckContext::from_frame(frames[0]);
        RedundantDisagreement::default().run(frames, &group, &ctx).unwrap()
    }

    /// Minutes `[from, to)` of the first day.
    fn during(from: usize, to: usize, by: f64) -> impl Fn(usize) -> f64 {
        move |i| if (from..to).contains(&i) { by } else { 0.0 }
    }

    #[test]
    fn two_of_three_names_suspect() {
        let a = sensor("pt-101a", 1, 1.0, |_| 0.0);
        let b = sensor("pt-101b", 2, 1.0, during(600, 960, 3.0)); // +3 bar for 6 h
        let c = sensor("pt-101c", 3, 1.0, |_| 0.0);
        let out = run(&[&a, &b, &c], serde_json::json!({"tolerance": 1.0}));
        assert_eq!(out.findings.len(), 1, "{:?}", out.findings);
        let f = &out.findings[0];
        assert_eq!(f.series_id, "pt-101b");
        assert_eq!(f.evidence["suspect"], "pt-101b");
        assert_eq!(f.evidence["tolerance_source"], "param");
        assert_eq!((f.window.start, f.window.end), (T0 + 600 * NS_PER_MIN, T0 + 960 * NS_PER_MIN));
        assert!((f.evidence["max_abs_diff"].as_f64().unwrap() - 3.0).abs() < 0.3);
        assert_eq!(
            f.summary,
            format!(
                "PT-101B reads up to {} bar away from PT-101A/PT-101C for 6h (tolerance 1 bar)",
                num(f.evidence["max_abs_diff"].as_f64().unwrap())
            )
        );
        // One metric point per member and day, named after the group.
        assert_eq!(out.metrics.iter().filter(|m| m.name == "max_abs_diff:g-pt").count(), 3 * 2);
    }

    #[test]
    fn pair_without_suspect() {
        let a = sensor("ft-1", 1, 1.0, |_| 0.0);
        let b = sensor("ft-2", 2, 1.0, during(300, 330, 2.0));
        let out = run(&[&a, &b], serde_json::json!({"tolerance": 1.0}));
        assert_eq!(out.findings.len(), 1, "{:?}", out.findings);
        let f = &out.findings[0];
        assert_eq!(f.series_id, "ft-1");
        assert!(f.evidence["suspect"].is_null());
        assert_eq!(f.evidence["duration_ns"], 30 * NS_PER_MIN);
        assert!(f.summary.ends_with("cannot tell which is off"), "{}", f.summary);
    }

    #[test]
    fn short_blip_ignored() {
        let a = sensor("ft-1", 1, 1.0, |_| 0.0);
        let b = sensor("ft-2", 2, 1.0, during(300, 310, 2.0));
        assert!(run(&[&a, &b], serde_json::json!({"tolerance": 1.0})).findings.is_empty());
        // Two 10-minute blips 5 minutes apart stay below the minimum: short runs go first.
        let c = sensor("ft-2", 2, 1.0, |i| {
            if (300..310).contains(&i) || (315..325).contains(&i) {
                2.0
            } else {
                0.0
            }
        });
        assert!(run(&[&a, &c], serde_json::json!({"tolerance": 1.0})).findings.is_empty());
        // Two 20-minute disagreements 10 minutes apart merge into one episode.
        let d = sensor("ft-2", 2, 1.0, |i| {
            if (300..320).contains(&i) || (330..350).contains(&i) {
                2.0
            } else {
                0.0
            }
        });
        let out = run(&[&a, &d], serde_json::json!({"tolerance": 1.0}));
        assert_eq!(out.findings.len(), 1);
        assert_eq!(out.findings[0].window.duration(), 50 * NS_PER_MIN);
    }

    #[test]
    fn auto_tolerance_catches_bias() {
        let a = sensor("tt-1", 1, 1.0, |_| 0.0);
        let b = sensor("tt-2", 2, 1.0, |_| 0.5); // a constant half-bar bias, noise 0.05
        let out = run(&[&a, &b], serde_json::Value::Null);
        assert_eq!(out.findings.len(), 1, "{:?}", out.findings);
        let f = &out.findings[0];
        assert_eq!(f.evidence["tolerance_source"], "auto");
        assert!(f.evidence["tolerance"].as_f64().unwrap() < 0.5);
        assert_eq!(f.window.duration(), N as i64 * NS_PER_MIN);
        // Without a bias the same noise stays silent.
        let c = sensor("tt-2", 2, 1.0, |_| 0.0);
        assert!(run(&[&a, &c], serde_json::Value::Null).findings.is_empty());
    }

    #[test]
    fn explicit_tolerance() {
        // Two sensors designed to differ by 5 (different tap points): auto flags it, a group
        // tolerance of 6 accepts it.
        let a = sensor("pt-in", 1, 1.0, |_| 0.0);
        let b = sensor("pt-out", 2, 1.0, |_| -5.0);
        assert_eq!(run(&[&a, &b], serde_json::Value::Null).findings.len(), 1);
        assert!(run(&[&a, &b], serde_json::json!({"tolerance": 6.0})).findings.is_empty());
    }

    #[test]
    fn pct_tolerance() {
        // Around 1000 units; a 20-unit (2 %) offset for an hour.
        let a = sensor("m-1", 1, 20.0, |_| 0.0);
        let b = sensor("m-2", 2, 20.0, during(120, 180, 20.0));
        let out = run(&[&a, &b], serde_json::json!({"tolerance_pct": 1.0}));
        assert_eq!(out.findings.len(), 1);
        assert_eq!(out.findings[0].evidence["tolerance_source"], "pct");
        assert!((out.findings[0].evidence["tolerance"].as_f64().unwrap() - 10.0).abs() < 1.0);
        assert!(run(&[&a, &b], serde_json::json!({"tolerance_pct": 5.0})).findings.is_empty());
        // `tolerance` wins over `tolerance_pct`.
        let out = run(&[&a, &b], serde_json::json!({"tolerance": 50.0, "tolerance_pct": 1.0}));
        assert!(out.findings.is_empty());
    }

    #[test]
    fn sparse_bins_ignored() {
        // b and c are missing for two hours while a reads far off: one member alone cannot
        // disagree with anyone.
        let a = sensor("pt-a", 1, 1.0, during(600, 720, 30.0));
        let mut b = sensor("pt-b", 2, 1.0, |_| 0.0);
        let mut c = sensor("pt-c", 3, 1.0, |_| 0.0);
        for i in 600..720 {
            b.values[i] = f64::NAN;
            c.values[i] = f64::NAN;
        }
        assert!(run(&[&a, &b, &c], serde_json::json!({"tolerance": 1.0})).findings.is_empty());
        // With only two of three present the bin still counts, but names no suspect.
        let mut c2 = sensor("pt-c", 3, 1.0, |_| 0.0);
        for i in 600..720 {
            c2.values[i] = f64::NAN;
        }
        let out = run(&[&a, &sensor("pt-b", 2, 1.0, |_| 0.0), &c2], serde_json::json!({"tolerance": 1.0}));
        assert_eq!(out.findings.len(), 1);
        assert!(out.findings[0].evidence["suspect"].is_null());
    }

    #[test]
    fn bad_params_are_invalid() {
        let a = sensor("x", 1, 1.0, |_| 0.0);
        let b = sensor("y", 2, 1.0, |_| 0.0);
        let group = |params| SeriesGroup {
            id: "g".into(),
            name: "G".into(),
            kind: GroupKind::Redundant,
            members: ["x", "y"]
                .iter()
                .map(|s| GroupMember { series_id: s.to_string(), role: MemberRole::Member })
                .collect(),
            params,
        };
        let ctx = CheckContext::from_frame(&a);
        for (params, message) in [
            (serde_json::json!({"tolerance": -1.0}), "tolerance must be a positive"),
            (serde_json::json!({"tolerance_pct": 0.0}), "tolerance_pct must be a positive"),
            (serde_json::json!({"grid": "0s"}), "grid must be positive"),
            (serde_json::json!({"min_duration": "soon"}), "cannot parse duration"),
            (serde_json::json!({"k": "big"}), "invalid type"),
        ] {
            let err = RedundantDisagreement::default().run(&[&a, &b], &group(params), &ctx).unwrap_err();
            assert!(matches!(err, Error::InvalidParams { .. }) && err.to_string().contains(message), "{err}");
        }
    }

    #[test]
    fn registry_runs_it_on_redundant_groups_only() {
        use crate::registry::{CheckConfig, Registry};
        use std::collections::BTreeMap;
        let frames = vec![sensor("ft-1", 1, 1.0, |_| 0.0), sensor("ft-2", 2, 1.0, during(300, 360, 2.0))];
        let ctx = CheckContext::from_frame(&frames[0]);
        let configs =
            vec![CheckConfig { id: ID.into(), params: serde_json::json!({"tolerance": 1.0}), enabled: true }];
        let make = |kind| SeriesGroup {
            id: "g".into(),
            name: "G".into(),
            kind,
            members: ["ft-1", "ft-2"]
                .iter()
                .map(|s| GroupMember { series_id: s.to_string(), role: MemberRole::Member })
                .collect(),
            params: serde_json::Value::Null,
        };
        let out =
            Registry::run_multi(&configs, &frames, &BTreeMap::new(), &[make(GroupKind::Redundant)], &ctx)
                .unwrap();
        assert_eq!(out.per_series["ft-1"].findings.len(), 1);
        let out = Registry::run_multi(&configs, &frames, &BTreeMap::new(), &[make(GroupKind::Related)], &ctx)
            .unwrap();
        assert!(out.per_series["ft-1"].findings.is_empty());
        assert!(Registry::default_multi_configs().iter().any(|c| c.id == ID));
    }

    #[test]
    fn pct_tolerance_on_idle_members_is_floored() {
        // Two flow meters on an idle line read 0 most of the time, with tiny quantised noise:
        // the median magnitude is 0, so the percentage alone would tolerate nothing.
        let meter = |id: &str, seed: u64| {
            let mut rng = Rng::new(seed);
            let values = (0..N)
                .map(|i| if i < N / 4 { 10.0 } else { 0.0 } + (rng.normal() * 2.0).round() * 0.01)
                .collect();
            let ts = (0..N as i64).map(|i| T0 + i * NS_PER_MIN).collect();
            SeriesFrame::with_default_quality(SeriesMeta::new(id), ts, values).unwrap()
        };
        let (a, b) = (meter("fq-1", 1), meter("fq-2", 2));
        let out = run(&[&a, &b], serde_json::json!({"tolerance_pct": 1.0}));
        assert!(out.findings.is_empty(), "{:?}", out.findings);
    }
}
