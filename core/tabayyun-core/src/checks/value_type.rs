//! `tby.value_type` — NaN, Inf and non-numeric values (catalogue #7).

use super::{expected_interval, metric, run_window, runs_where, Check, CheckContext, CheckOutput};
use crate::error::Result;
use crate::finding::{Dimension, Finding, Severity};
use crate::frame::SeriesFrame;
use serde::{Deserialize, Serialize};

pub const ID: &str = "tby.value_type";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct ValueType {
    /// NaN share above which a finding is raised.
    pub max_nan: f64,
    pub severity: Severity,
}

impl Default for ValueType {
    fn default() -> Self {
        Self { max_nan: 0.01, severity: Severity::High }
    }
}

impl Check for ValueType {
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
        if n == 0 {
            return Ok(out);
        }
        let interval = expected_interval(&f, ctx).unwrap_or(1);
        let nan = f.values.iter().filter(|v| v.is_nan()).count();
        let inf = f.values.iter().filter(|v| v.is_infinite()).count();
        out.metrics.push(metric(ID, &f, "nan_ratio", ctx.window.end, nan as f64 / n as f64));
        out.metrics.push(metric(ID, &f, "inf_count", ctx.window.end, inf as f64));

        if inf > 0 {
            for (s, e) in runs_where(n, |i| f.values[i].is_infinite()) {
                let w = run_window(&f, s, e, interval);
                out.findings.push(Finding::new(
                    ID,
                    &f.meta.id,
                    Dimension::Validity,
                    Severity::Critical,
                    w,
                    (e - s) as f64 / n as f64,
                    format!("{} infinite values", e - s),
                    serde_json::json!({"inf_count": e - s}),
                ));
            }
        }
        let nan_ratio = nan as f64 / n as f64;
        if nan_ratio > self.max_nan {
            for (s, e) in runs_where(n, |i| f.values[i].is_nan()) {
                let w = run_window(&f, s, e, interval);
                out.findings.push(Finding::new(
                    ID,
                    &f.meta.id,
                    Dimension::Validity,
                    self.severity,
                    w,
                    (e - s) as f64 / n as f64,
                    format!(
                        "{} null/NaN values in a row (NaN share over window {:.1}%, limit {:.1}%)",
                        e - s,
                        nan_ratio * 100.0,
                        self.max_nan * 100.0
                    ),
                    serde_json::json!({"nan_run": e - s, "nan_ratio": nan_ratio, "max_nan": self.max_nan}),
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
    use crate::synth::inject;

    #[test]
    fn nan_runs_and_inf() {
        let mut f = base(1000);
        inject::nans(&mut f, 100, 50);
        inject::set(&mut f, 500, 2, f64::INFINITY);
        let out = ValueType::default().run(&f, &ctx(&f)).unwrap();
        let fs = ids(&out, ID);
        assert!(fs.iter().any(|x| x.evidence["nan_run"] == 50));
        assert!(fs.iter().any(|x| x.evidence["inf_count"] == 2 && x.severity == Severity::Critical));
    }

    #[test]
    fn few_nans_tolerated() {
        let mut f = base(1000);
        inject::nans(&mut f, 100, 5);
        let out = ValueType::default().run(&f, &ctx(&f)).unwrap();
        assert!(ids(&out, ID).is_empty());
    }
}
