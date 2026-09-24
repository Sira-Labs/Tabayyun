//! `tby.physical_range` — values outside physically possible limits (catalogue #9).
//!
//! Excursion runs (consecutive samples outside the limits) that start less than `cluster_gap`
//! after the previous run ends form one episode and one finding with the exact count; with
//! more episodes than `max_findings` one low-severity summary replaces them (ADR-0011,
//! spec 016).

use super::{
    duration_param, expected_interval, metric, run_window, runs_where, Check, CheckContext, CheckOutput,
};
use crate::error::{Error, Result};
use crate::finding::{Dimension, Finding, Severity, Window};
use crate::frame::SeriesFrame;
use crate::time::{format_duration, NS_PER_HOUR};
use serde::{Deserialize, Serialize};

/// Episodes listed in the summary finding.
const MAX_LISTED: usize = 5;

pub const ID: &str = "tby.physical_range";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct PhysicalRange {
    /// Override limits; otherwise series metadata / unit defaults are used.
    pub min: Option<f64>,
    pub max: Option<f64>,
    /// Excursions closer than this form one episode (`auto` = max(1 h, 12 × interval)).
    pub cluster_gap: String,
    /// More episodes than this collapse into one summary finding for the whole window.
    pub max_findings: usize,
    pub severity: Severity,
}

impl Default for PhysicalRange {
    fn default() -> Self {
        Self {
            min: None,
            max: None,
            cluster_gap: "auto".into(),
            max_findings: 20,
            severity: Severity::Critical,
        }
    }
}

/// One episode: its excursion runs `[start, end)` and their windows.
struct Episode {
    runs: Vec<(usize, usize, Window)>,
}

impl Episode {
    fn window(&self) -> Window {
        Window::new(self.runs[0].2.start, self.runs[self.runs.len() - 1].2.end)
    }
    fn count(&self) -> usize {
        self.runs.iter().map(|(s, e, _)| e - s).sum()
    }
}

impl Check for PhysicalRange {
    fn id(&self) -> &'static str {
        ID
    }
    fn dimension(&self) -> Dimension {
        Dimension::Validity
    }
    fn default_severity(&self) -> Severity {
        self.severity
    }

    fn run(&self, frame: &SeriesFrame, ctx: &CheckContext) -> Result<CheckOutput> {
        let (meta_min, meta_max) = frame.meta.physical_limits();
        let lo = self.min.or(meta_min);
        let hi = self.max.or(meta_max);
        if lo.is_none() && hi.is_none() {
            return Err(Error::MissingMetadata {
                check: ID.into(),
                field: "physical_min/physical_max (or unit)",
            });
        }
        let mut out = CheckOutput::default();
        let (f, _) = frame.normalized();
        let n = f.len();
        if n == 0 {
            return Ok(out);
        }
        let interval = expected_interval(&f, ctx).unwrap_or(1).max(1);
        let cluster_gap = if self.cluster_gap == "auto" {
            (12 * interval).max(NS_PER_HOUR)
        } else {
            duration_param(ID, "cluster_gap", &self.cluster_gap)?
        };
        if cluster_gap <= 0 {
            return Err(Error::InvalidParams {
                check: ID.into(),
                reason: "cluster_gap must be positive".into(),
            });
        }
        let outside = |v: f64| v.is_finite() && (lo.is_some_and(|l| v < l) || hi.is_some_and(|h| v > h));
        let runs = runs_where(n, |i| outside(f.values[i]));
        let count: usize = runs.iter().map(|(s, e)| e - s).sum();
        out.metrics.push(metric(ID, &f, "out_of_range_ratio", ctx.window.end, count as f64 / n as f64));

        // Runs starting less than `cluster_gap` after the previous run's end join its episode.
        let mut episodes: Vec<Episode> = Vec::new();
        for (s, e) in runs {
            let w = run_window(&f, s, e, interval);
            match episodes.last_mut() {
                Some(ep) if w.start - ep.window().end < cluster_gap => ep.runs.push((s, e, w)),
                _ => episodes.push(Episode { runs: vec![(s, e, w)] }),
            }
        }
        out.metrics.push(metric(ID, &f, "range_episodes", ctx.window.end, episodes.len() as f64));
        if episodes.is_empty() {
            return Ok(out);
        }
        let observed = |runs: &[(usize, usize, Window)]| {
            runs.iter()
                .flat_map(|&(s, e, _)| f.values[s..e].iter().copied())
                .fold((f64::INFINITY, f64::NEG_INFINITY), |(a, b), v| (a.min(v), b.max(v)))
        };

        if episodes.len() > self.max_findings {
            let mut top: Vec<&Episode> = episodes.iter().collect();
            top.sort_by(|a, b| b.count().cmp(&a.count()).then(a.window().start.cmp(&b.window().start)));
            let all: Vec<(usize, usize, Window)> =
                episodes.iter().flat_map(|ep| ep.runs.iter().copied()).collect();
            let (vmin, vmax) = observed(&all);
            let w = Window::new(episodes[0].window().start, episodes[episodes.len() - 1].window().end);
            out.findings.push(Finding::new(
                ID, &f.meta.id, Dimension::Validity, Severity::Low, w,
                count as f64 / n as f64,
                format!(
                    "Values outside physical limits [{}, {}] in {} episodes over {}: {count} values ({:.2} % of samples, observed {vmin} to {vmax}); episodes are not listed individually",
                    fmt(lo), fmt(hi), episodes.len(), format_duration(w.duration()), 100.0 * count as f64 / n as f64
                ),
                serde_json::json!({"n_episodes": episodes.len(), "count": count, "share": count as f64 / n as f64,
                    "min_observed": vmin, "max_observed": vmax, "limit_min": lo, "limit_max": hi,
                    "cluster_gap_ns": cluster_gap, "max_findings": self.max_findings,
                    "largest": top.iter().take(MAX_LISTED).map(|ep| {
                        let w = ep.window();
                        serde_json::json!({"start": w.start, "end": w.end, "count": ep.count(), "n_excursions": ep.runs.len()})
                    }).collect::<Vec<_>>()}),
            ));
            return Ok(out);
        }

        for ep in &episodes {
            let w = ep.window();
            let (vmin, vmax) = observed(&ep.runs);
            let (c, k) = (ep.count(), ep.runs.len());
            let summary = if k == 1 {
                format!(
                    "{c} values outside physical limits [{}, {}] (observed {vmin} to {vmax})",
                    fmt(lo),
                    fmt(hi)
                )
            } else {
                format!(
                    "{k} excursions ({c} values) outside physical limits [{}, {}] within {} (observed {vmin} to {vmax})",
                    fmt(lo), fmt(hi), format_duration(w.duration())
                )
            };
            out.findings.push(Finding::new(
                ID, &f.meta.id, Dimension::Validity, self.severity, w,
                c as f64 / n as f64,
                summary,
                serde_json::json!({"count": c, "min_observed": vmin, "max_observed": vmax, "limit_min": lo, "limit_max": hi,
                    "n_excursions": k, "first_ts": w.start, "last_ts": ep.runs[k - 1].2.end, "cluster_gap_ns": cluster_gap}),
            ));
        }
        Ok(out)
    }
}

fn fmt(v: Option<f64>) -> String {
    v.map(|x| x.to_string()).unwrap_or_else(|| "-".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::checks::testutil::*;
    use crate::synth::inject;

    #[test]
    fn needs_limits() {
        let f = base(100);
        assert!(matches!(PhysicalRange::default().run(&f, &ctx(&f)), Err(Error::MissingMetadata { .. })));
    }

    #[test]
    fn unit_default_limits() {
        let mut f = base(1000);
        f.meta.unit = Some("%".into());
        inject::set(&mut f, 10, 5, 250.0);
        let out = PhysicalRange::default().run(&f, &ctx(&f)).unwrap();
        let fs = ids(&out, ID);
        assert_eq!(fs.len(), 1);
        assert_eq!(fs[0].evidence["count"], 5);
        assert_eq!(fs[0].evidence["n_excursions"], 1);
        assert_eq!(fs[0].severity, Severity::Critical);
        assert_eq!(fs[0].summary, "5 values outside physical limits [0, 100] (observed 250 to 250)");
    }

    fn percent(n: usize) -> SeriesFrame {
        let mut f = base(n);
        f.meta.unit = Some("%".into());
        f
    }

    #[test]
    fn clustered_excursions_one_finding() {
        // 34 three-minute excursions, each 3 minutes after the previous one ends (1-min data).
        let mut f = percent(1440);
        for k in 0..34 {
            inject::set(&mut f, 300 + 6 * k, 3, 120.0 + k as f64);
        }
        let out = PhysicalRange::default().run(&f, &ctx(&f)).unwrap();
        let fs = ids(&out, ID);
        assert_eq!(fs.len(), 1, "{:?}", fs);
        let e = &fs[0].evidence;
        assert_eq!(e["n_excursions"], 34);
        assert_eq!(e["count"], 102);
        assert_eq!(e["max_observed"], 153.0);
        assert_eq!(fs[0].severity, Severity::Critical);
        assert_eq!((fs[0].window.start, fs[0].window.end), (f.ts[300], f.ts[300 + 6 * 33 + 3]));
        assert_eq!(e["first_ts"], f.ts[300]);
        assert_eq!(e["last_ts"], f.ts[300 + 6 * 33 + 3]);
        assert!(
            fs[0].summary.starts_with("34 excursions (102 values) outside physical limits [0, 100] within"),
            "{}",
            fs[0].summary
        );
        // An explicit gap shorter than the 3-minute gaps keeps them apart: 34 episodes, more
        // than `max_findings`, so one summary; with a higher cap they are listed one by one.
        let split = PhysicalRange { cluster_gap: "2m".into(), ..Default::default() };
        let out = split.run(&f, &ctx(&f)).unwrap();
        assert_eq!(ids(&out, ID).len(), 1);
        assert_eq!(ids(&out, ID)[0].evidence["n_episodes"], 34);
        let out = PhysicalRange { max_findings: 40, ..split }.run(&f, &ctx(&f)).unwrap();
        assert_eq!(ids(&out, ID).len(), 34);
    }

    #[test]
    fn distant_excursions_separate() {
        let mut f = percent(3000);
        inject::set(&mut f, 100, 4, 130.0);
        inject::set(&mut f, 100 + 1440, 2, -5.0);
        let out = PhysicalRange::default().run(&f, &ctx(&f)).unwrap();
        let fs = ids(&out, ID);
        assert_eq!(fs.len(), 2);
        assert_eq!((fs[0].evidence["count"].as_u64(), fs[1].evidence["count"].as_u64()), (Some(4), Some(2)));
        assert_eq!(out.metrics.iter().find(|m| m.name == "range_episodes").unwrap().value, 2.0);
    }

    #[test]
    fn many_episodes_summary() {
        // 50 episodes 90 minutes apart, beyond the 1-hour cluster gap.
        let mut f = percent(50 * 90 + 100);
        for k in 0..50 {
            inject::set(&mut f, 50 + 90 * k, 1 + k % 3, 200.0 + k as f64);
        }
        let out = PhysicalRange::default().run(&f, &ctx(&f)).unwrap();
        let fs = ids(&out, ID);
        assert_eq!(fs.len(), 1, "{:?}", fs);
        let e = &fs[0].evidence;
        assert_eq!(fs[0].severity, Severity::Low);
        assert_eq!(e["n_episodes"], 50);
        let count: u64 = (0..50).map(|k| 1 + k % 3).sum();
        assert_eq!(e["count"], count);
        assert_eq!(e["max_observed"], 249.0);
        assert_eq!(e["largest"].as_array().unwrap().len(), 5);
        assert_eq!(e["largest"][0]["count"], 3);
        assert!(fs[0].summary.contains("in 50 episodes"), "{}", fs[0].summary);
    }

    #[test]
    fn bad_cluster_gap_is_invalid() {
        let mut f = percent(100);
        inject::set(&mut f, 10, 2, 150.0);
        for gap in ["soon", "0s"] {
            let err = PhysicalRange { cluster_gap: gap.into(), ..Default::default() }
                .run(&f, &ctx(&f))
                .unwrap_err();
            assert!(matches!(err, Error::InvalidParams { .. }), "{err}");
        }
    }
}
