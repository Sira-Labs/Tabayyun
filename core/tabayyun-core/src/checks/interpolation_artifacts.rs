//! `tby.interpolation_artifacts` — perfectly linear runs that betray interpolation or
//! over-aggressive historian compression (catalogue #17).

use super::{expected_interval, metric, run_window, Check, CheckContext, CheckOutput};
use crate::error::Result;
use crate::finding::{Dimension, Finding, Severity};
use crate::frame::SeriesFrame;
use crate::profile::{linear_runs, resolution};
use serde::{Deserialize, Serialize};

pub const ID: &str = "tby.interpolation_artifacts";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct InterpolationArtifacts {
    /// Minimum consecutive samples on a straight line.
    pub window: usize,
    /// Share of samples in linear runs above which findings are raised.
    pub max_share: f64,
    /// Tolerance on the second difference; `None` = half the series resolution.
    pub atol: Option<f64>,
    pub max_run_findings: usize,
    pub severity: Severity,
}

impl Default for InterpolationArtifacts {
    fn default() -> Self {
        Self { window: 6, max_share: 0.20, atol: None, max_run_findings: 200, severity: Severity::Medium }
    }
}

impl Check for InterpolationArtifacts {
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
        let mut out = CheckOutput::default();
        let (f, _) = frame.normalized();
        let n = f.len();
        if n < self.window {
            return Ok(out);
        }
        let interval = expected_interval(&f, ctx).unwrap_or(1);
        let atol = self
            .atol
            .or(frame.meta.resolution.map(|r| r / 2.0))
            .or(resolution(&f.values).map(|r| r / 2.0))
            .unwrap_or(1e-9);
        let runs = linear_runs(&f.values, self.window, atol);
        let total: usize = runs.iter().map(|(s, e)| e - s).sum();
        let share = total as f64 / n as f64;
        out.metrics.push(metric(ID, &f, "linear_share", ctx.window.end, share));
        if share <= self.max_share {
            return Ok(out);
        }
        for (k, (s, e)) in runs.iter().enumerate() {
            if k >= self.max_run_findings {
                break;
            }
            let w = run_window(&f, *s, *e, interval);
            out.findings.push(Finding::new(
                ID, &f.meta.id, Dimension::Validity, self.severity, w,
                (e - s) as f64 / n as f64,
                format!("{} samples lie on a perfect straight line (interpolated or compressed, {:.0}% of the window is linear)", e - s, share * 100.0),
                serde_json::json!({"run_samples": e - s, "linear_share": share, "atol": atol, "window": self.window}),
            ));
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::checks::testutil::*;

    #[test]
    fn interpolated_third_flagged() {
        let mut f = base(1500);
        // Replace samples 500..1000 with a linear interpolation between the endpoints.
        let (a, b) = (f.values[500], f.values[1000]);
        for i in 500..1000 {
            f.values[i] = a + (b - a) * (i - 500) as f64 / 500.0;
        }
        let out = InterpolationArtifacts::default().run(&f, &ctx(&f)).unwrap();
        let fs = ids(&out, ID);
        assert_eq!(fs.len(), 1, "{:?}", out.findings);
        assert!(fs[0].evidence["run_samples"].as_u64().unwrap() >= 495);
    }

    #[test]
    fn noisy_signal_passes() {
        let f = base(1500);
        assert!(ids(&InterpolationArtifacts::default().run(&f, &ctx(&f)).unwrap(), ID).is_empty());
    }
}
