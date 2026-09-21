//! `tby.distribution_drift` — PSI and normalised Wasserstein distance per segment against the
//! baseline quantile grid (catalogue #19).

use super::{baseline, metric, segments, usable_values, Check, CheckContext, CheckOutput};
use crate::error::Result;
use crate::finding::{Dimension, Finding, Severity};
use crate::frame::SeriesFrame;
use crate::profile::quantile_f64;
use crate::time::NS_PER_DAY;
use serde::{Deserialize, Serialize};

pub const ID: &str = "tby.distribution_drift";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct DistributionDrift {
    pub segment_ns: i64,
    /// Population stability index thresholds (10 baseline-decile bins).
    pub psi_warn: f64,
    pub psi_alert: f64,
    /// Wasserstein-1 distance normalised by the baseline inter-quartile range.
    pub wasserstein_alert: f64,
    pub min_samples: usize,
    pub severity: Severity,
}

impl Default for DistributionDrift {
    fn default() -> Self {
        Self {
            segment_ns: NS_PER_DAY,
            psi_warn: 0.10,
            psi_alert: 0.25,
            wasserstein_alert: 0.10,
            min_samples: 100,
            severity: Severity::Medium,
        }
    }
}

/// PSI of `sorted` against decile edges `edges` (9 interior edges from the baseline).
fn psi(sorted: &[f64], edges: &[f64]) -> f64 {
    let n = sorted.len() as f64;
    let mut psi = 0.0;
    let mut lo = 0usize;
    for b in 0..=edges.len() {
        let hi = if b < edges.len() { sorted.partition_point(|v| *v <= edges[b]) } else { sorted.len() };
        let actual = ((hi - lo) as f64 / n).max(1e-4);
        let expected = 1.0 / (edges.len() + 1) as f64;
        psi += (actual - expected) * (actual / expected).ln();
        lo = hi;
    }
    psi
}

impl Check for DistributionDrift {
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
        let (profile, source) = baseline(ctx, &f);
        if profile.quantiles.len() != 21 {
            return Ok(out);
        }
        let q = &profile.quantiles;
        let iqr = (q[15] - q[5]).max(profile.resolution.unwrap_or(0.0)).max(1e-12);
        let edges: Vec<f64> = (1..10).map(|k| q[2 * k]).collect(); // deciles 10..90 %
        for (s, e, w) in segments(&f, self.segment_ns) {
            let mut seg = usable_values(&f, s, e);
            if seg.len() < self.min_samples {
                continue;
            }
            seg.sort_by(|a, b| a.partial_cmp(b).unwrap());
            let psi_v = psi(&seg, &edges);
            let wass = (0..=20).map(|k| (quantile_f64(&seg, k as f64 / 20.0) - q[k]).abs()).sum::<f64>()
                / 21.0
                / iqr;
            out.metrics.push(metric(ID, &f, "psi", w.end, psi_v));
            out.metrics.push(metric(ID, &f, "wasserstein_norm", w.end, wass));
            let alert = psi_v >= self.psi_alert || wass >= self.wasserstein_alert;
            let warn = psi_v >= self.psi_warn;
            if alert || warn {
                out.findings.push(Finding::new(
                    ID, &f.meta.id, Dimension::Plausibility, if alert { self.severity } else { Severity::Low }, w,
                    ctx.window.overlap_fraction(&w),
                    format!("Value distribution shifted: PSI {psi_v:.3} (warn {:.2}, alert {:.2}), normalised Wasserstein {wass:.3}",
                        self.psi_warn, self.psi_alert),
                    serde_json::json!({"psi": psi_v, "wasserstein_norm": wass, "psi_warn": self.psi_warn, "psi_alert": self.psi_alert,
                        "wasserstein_alert": self.wasserstein_alert, "baseline": source}),
                ));
            }
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::checks::testutil::*;
    use crate::Profile;

    #[test]
    fn shifted_day_flagged() {
        let profile = Profile::compute(&base(3 * 1440));
        let mut f = base(3 * 1440);
        for v in f.values.iter_mut().skip(2880) {
            *v += 6.0; // shift by ~0.5 amplitude on day 3
        }
        let c = ctx(&f).with_profile(profile);
        let out = DistributionDrift::default().run(&f, &c).unwrap();
        let fs = ids(&out, ID);
        assert_eq!(fs.len(), 1, "{:?}", out.findings);
        assert_eq!(fs[0].severity, Severity::Medium);
    }

    #[test]
    fn same_distribution_passes() {
        let profile = Profile::compute(&base(3 * 1440));
        let f = base(3 * 1440);
        let c = ctx(&f).with_profile(profile);
        let out = DistributionDrift::default().run(&f, &c).unwrap();
        // Day-to-day sinusoid phases are identical, so daily distributions match the 3-day one.
        assert!(ids(&out, ID).iter().all(|x| x.severity == Severity::Low), "{:?}", out.findings);
    }
}
