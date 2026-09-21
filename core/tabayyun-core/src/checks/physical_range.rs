//! `tby.physical_range` — values outside physically possible limits (catalogue #9).

use super::{expected_interval, metric, run_window, runs_where, Check, CheckContext, CheckOutput};
use crate::error::{Error, Result};
use crate::finding::{Dimension, Finding, Severity};
use crate::frame::SeriesFrame;
use serde::{Deserialize, Serialize};

pub const ID: &str = "tby.physical_range";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct PhysicalRange {
    /// Override limits; otherwise series metadata / unit defaults are used.
    pub min: Option<f64>,
    pub max: Option<f64>,
    pub severity: Severity,
}

impl Default for PhysicalRange {
    fn default() -> Self {
        Self { min: None, max: None, severity: Severity::Critical }
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
        let interval = expected_interval(&f, ctx).unwrap_or(1);
        let outside = |v: f64| v.is_finite() && (lo.is_some_and(|l| v < l) || hi.is_some_and(|h| v > h));
        let runs = runs_where(n, |i| outside(f.values[i]));
        let count: usize = runs.iter().map(|(s, e)| e - s).sum();
        out.metrics.push(metric(ID, &f, "out_of_range_ratio", ctx.window.end, count as f64 / n as f64));
        for (s, e) in runs {
            let w = run_window(&f, s, e, interval);
            let seg = &f.values[s..e];
            let vmin = seg.iter().cloned().fold(f64::INFINITY, f64::min);
            let vmax = seg.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
            out.findings.push(Finding::new(
                ID, &f.meta.id, Dimension::Validity, self.severity, w,
                (e - s) as f64 / n as f64,
                format!("{} values outside physical limits [{}, {}] (observed {} to {})",
                    e - s, fmt(lo), fmt(hi), vmin, vmax),
                serde_json::json!({"count": e - s, "min_observed": vmin, "max_observed": vmax, "limit_min": lo, "limit_max": hi}),
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
        assert_eq!(fs[0].severity, Severity::Critical);
    }
}
