//! `tby.non_negative` — negative values for non-negative quantities (catalogue #11).

use super::{expected_interval, metric, run_window, runs_where, Check, CheckContext, CheckOutput};
use crate::error::{Error, Result};
use crate::finding::{Dimension, Finding, Severity};
use crate::frame::SeriesFrame;
use crate::profile::resolution;
use serde::{Deserialize, Serialize};

pub const ID: &str = "tby.non_negative";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct NonNegative {
    /// Values below `-tolerance` are findings; `None` = series resolution.
    pub tolerance: Option<f64>,
    /// Run even if metadata does not say the series is non-negative.
    pub force: bool,
    pub severity: Severity,
}

impl Default for NonNegative {
    fn default() -> Self {
        Self { tolerance: None, force: false, severity: Severity::High }
    }
}

impl Check for NonNegative {
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
        if !self.force && !frame.meta.is_non_negative() {
            return Err(Error::MissingMetadata { check: ID.into(), field: "non_negative (or unit)" });
        }
        let mut out = CheckOutput::default();
        let (f, _) = frame.normalized();
        let n = f.len();
        if n == 0 {
            return Ok(out);
        }
        let interval = expected_interval(&f, ctx).unwrap_or(1);
        let tol = self.tolerance.or(frame.meta.resolution).or_else(|| resolution(&f.values)).unwrap_or(0.0);
        let runs = runs_where(n, |i| f.values[i].is_finite() && f.values[i] < -tol);
        let count: usize = runs.iter().map(|(s, e)| e - s).sum();
        out.metrics.push(metric(ID, &f, "negative_ratio", ctx.window.end, count as f64 / n as f64));
        for (s, e) in runs {
            let w = run_window(&f, s, e, interval);
            let vmin = f.values[s..e].iter().cloned().fold(f64::INFINITY, f64::min);
            out.findings.push(Finding::new(
                ID,
                &f.meta.id,
                Dimension::Validity,
                self.severity,
                w,
                (e - s) as f64 / n as f64,
                format!("{} negative values (down to {}) for a non-negative quantity", e - s, vmin),
                serde_json::json!({"count": e - s, "min_observed": vmin, "tolerance": tol}),
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
    fn flags_negative_flow() {
        let mut f = base(1000);
        f.meta.unit = Some("m3/h".into());
        inject::set(&mut f, 400, 3, -5.0);
        let out = NonNegative::default().run(&f, &ctx(&f)).unwrap();
        assert_eq!(ids(&out, ID).len(), 1);
    }

    #[test]
    fn skipped_without_metadata() {
        let f = base(100);
        assert!(matches!(NonNegative::default().run(&f, &ctx(&f)), Err(Error::MissingMetadata { .. })));
        assert!(NonNegative { force: true, ..Default::default() }.run(&f, &ctx(&f)).is_ok());
    }
}
