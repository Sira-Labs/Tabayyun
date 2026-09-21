//! `tby.rate_of_change` — slew-rate violations (catalogue #14).

use super::{expected_interval, metric, run_window, runs_where, Check, CheckContext, CheckOutput};
use crate::error::Result;
use crate::finding::{Dimension, Finding, Severity};
use crate::frame::SeriesFrame;
use crate::profile::Profile;
use serde::{Deserialize, Serialize};

pub const ID: &str = "tby.rate_of_change";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct RateOfChange {
    /// Absolute limit in value units per second; `None` = `factor × baseline rate_p999`.
    pub max_rate: Option<f64>,
    pub factor: f64,
    pub max_run_findings: usize,
    pub severity: Severity,
}

impl Default for RateOfChange {
    fn default() -> Self {
        Self { max_rate: None, factor: 3.0, max_run_findings: 200, severity: Severity::Medium }
    }
}

impl Check for RateOfChange {
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
        if n < 3 {
            return Ok(out);
        }
        let interval = expected_interval(&f, ctx).unwrap_or(1);
        let gap_cut = 3 * interval;
        let (limit, source) = match self.max_rate {
            Some(r) => (r, "explicit"),
            None => {
                let (p, src) = match &ctx.profile {
                    Some(p) => (p.clone(), "baseline"),
                    None => (Profile::compute(&f), "self"),
                };
                match p.rate_p999 {
                    Some(r) if r > 0.0 => (self.factor * r, src),
                    _ => return Ok(out),
                }
            }
        };
        // rate[i] is the rate arriving at sample i (from i-1). Index 0 never violates.
        let mut rate = vec![0.0f64; n];
        for (i, r) in rate.iter_mut().enumerate().skip(1) {
            let dt = f.ts[i] - f.ts[i - 1];
            let (a, b) = (f.values[i - 1], f.values[i]);
            if dt > 0 && dt <= gap_cut && a.is_finite() && b.is_finite() {
                *r = (b - a).abs() / (dt as f64 / 1e9);
            }
        }
        let runs = runs_where(n, |i| rate[i] > limit);
        let count: usize = runs.iter().map(|(s, e)| e - s).sum();
        out.metrics.push(metric(ID, &f, "rate_violations", ctx.window.end, count as f64));
        for (k, (s, e)) in runs.iter().enumerate() {
            if k >= self.max_run_findings {
                break;
            }
            let w = run_window(&f, s.saturating_sub(1), *e, interval);
            let peak = rate[*s..*e].iter().cloned().fold(0.0, f64::max);
            out.findings.push(Finding::new(
                ID, &f.meta.id, Dimension::Plausibility, self.severity, w,
                (e - s) as f64 / n as f64,
                format!("Value changed at {peak:.4} units/s, above the limit of {limit:.4} units/s ({source})"),
                serde_json::json!({"peak_rate": peak, "limit": limit, "limit_source": source, "samples": e - s}),
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

    #[test]
    fn step_against_baseline_flagged() {
        let profile = Profile::compute(&base(2880));
        let mut f = base(2880);
        inject::set(&mut f, 1000, 30, 80.0); // step of ~30 units in one minute
        let c = ctx(&f).with_profile(profile);
        let out = RateOfChange::default().run(&f, &c).unwrap();
        let fs = ids(&out, ID);
        assert_eq!(fs.len(), 2, "{:?}", out.findings); // up-step and down-step
    }

    #[test]
    fn explicit_limit() {
        let f = base(1440);
        let out = RateOfChange { max_rate: Some(1e-6), ..Default::default() }.run(&f, &ctx(&f)).unwrap();
        assert!(!ids(&out, ID).is_empty());
        let out = RateOfChange { max_rate: Some(1e6), ..Default::default() }.run(&f, &ctx(&f)).unwrap();
        assert!(ids(&out, ID).is_empty());
    }
}
