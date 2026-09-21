//! `tby.operational_range` — values outside the learned operating band (catalogue #10).

use super::{baseline, expected_interval, metric, run_window, runs_where, Check, CheckContext, CheckOutput};
use crate::error::Result;
use crate::finding::{Dimension, Finding, Severity};
use crate::frame::SeriesFrame;
use serde::{Deserialize, Serialize};

pub const ID: &str = "tby.operational_range";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct OperationalRange {
    /// Band = [p0.1 − k·1.4826·MAD, p99.9 + k·1.4826·MAD] of the baseline.
    pub k: f64,
    /// Share of samples outside the band above which findings are raised.
    pub max_share: f64,
    pub max_run_findings: usize,
    pub severity: Severity,
}

impl Default for OperationalRange {
    fn default() -> Self {
        Self { k: 1.0, max_share: 0.005, max_run_findings: 100, severity: Severity::Medium }
    }
}

impl Check for OperationalRange {
    fn id(&self) -> &'static str {
        ID
    }
    fn dimension(&self) -> Dimension {
        Dimension::Plausibility
    }
    fn default_severity(&self) -> Severity {
        self.severity
    }

    fn run(&self, frame: &SeriesFrame, ctx: &CheckContext) -> Result<CheckOutput> {
        let mut out = CheckOutput::default();
        let (f, _) = frame.normalized();
        let n = f.len();
        if n < 20 {
            return Ok(out);
        }
        let (profile, source) = baseline(ctx, &f);
        let (Some(p001), Some(p999), Some(mad)) = (profile.p001, profile.p999, profile.mad) else {
            return Ok(out);
        };
        let margin = self.k * 1.4826 * mad;
        let (lo, hi) = (p001 - margin, p999 + margin);
        let interval = expected_interval(&f, ctx).unwrap_or(1);
        let runs = runs_where(n, |i| f.values[i].is_finite() && (f.values[i] < lo || f.values[i] > hi));
        let count: usize = runs.iter().map(|(s, e)| e - s).sum();
        let share = count as f64 / n as f64;
        out.metrics.push(metric(ID, &f, "out_of_band_share", ctx.window.end, share));
        if share <= self.max_share {
            return Ok(out);
        }
        for (i, (s, e)) in runs.iter().enumerate() {
            if i >= self.max_run_findings {
                break;
            }
            let w = run_window(&f, *s, *e, interval);
            let seg = &f.values[*s..*e];
            let vmin = seg.iter().cloned().fold(f64::INFINITY, f64::min);
            let vmax = seg.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
            out.findings.push(Finding::new(
                ID, &f.meta.id, Dimension::Plausibility, self.severity, w,
                (e - s) as f64 / n as f64,
                format!("{} values outside the usual operating band [{lo:.4}, {hi:.4}] (observed {vmin:.4} to {vmax:.4})", e - s),
                serde_json::json!({"count": e - s, "band_min": lo, "band_max": hi, "min_observed": vmin, "max_observed": vmax,
                    "share": share, "baseline": source, "k": self.k}),
            ));
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::checks::testutil::*;
    use crate::synth::inject;
    use crate::Profile;

    #[test]
    fn excursion_against_baseline() {
        let clean = base(2880);
        let profile = Profile::compute(&clean);
        let mut f = base(2880);
        inject::set(&mut f, 1000, 40, 95.0); // far above base 50 ± 10
        let c = ctx(&f).with_profile(profile);
        let out = OperationalRange::default().run(&f, &c).unwrap();
        let fs = ids(&out, ID);
        assert_eq!(fs.len(), 1, "{:?}", out.findings);
        assert_eq!(fs[0].evidence["baseline"], "baseline");
        assert_eq!(fs[0].evidence["count"], 40);
    }

    #[test]
    fn clean_passes_self_baseline() {
        let f = base(2880);
        assert!(ids(&OperationalRange::default().run(&f, &ctx(&f)).unwrap(), ID).is_empty());
    }
}
