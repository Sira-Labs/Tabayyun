//! `tby.quality_flags` — bad / uncertain / estimated flag share (catalogue #6).
//!
//! The "frozen good" cross-check (quality stays Good while the value is stuck) is emitted by
//! the engine when it correlates this check with `tby.flatline` findings; it is not
//! duplicated here.

use super::{expected_interval, metric, run_window, runs_where, Check, CheckContext, CheckOutput};
use crate::error::Result;
use crate::finding::{Dimension, Finding, Severity};
use crate::frame::SeriesFrame;
use crate::quality::Quality;
use serde::{Deserialize, Serialize};

pub const ID: &str = "tby.quality_flags";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct QualityFlags {
    pub max_bad: f64,
    pub max_uncertain: f64,
    pub max_estimated: f64,
    /// Cap on per-run findings; the rest is covered by the share finding.
    pub max_run_findings: usize,
    pub severity: Severity,
}

impl Default for QualityFlags {
    fn default() -> Self {
        Self {
            max_bad: 0.01,
            max_uncertain: 0.05,
            max_estimated: 0.10,
            max_run_findings: 100,
            severity: Severity::High,
        }
    }
}

impl Check for QualityFlags {
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
        let share = |q: Quality| f.quality.iter().filter(|x| **x == q).count() as f64 / n as f64;
        let (bad, uncertain, estimated) =
            (share(Quality::Bad), share(Quality::Uncertain), share(Quality::Estimated));
        out.metrics.push(metric(ID, &f, "bad_share", ctx.window.end, bad));
        out.metrics.push(metric(ID, &f, "uncertain_share", ctx.window.end, uncertain));
        out.metrics.push(metric(ID, &f, "estimated_share", ctx.window.end, estimated));

        for (q, s, limit, label, sev) in [
            (Quality::Bad, bad, self.max_bad, "bad", self.severity),
            (Quality::Uncertain, uncertain, self.max_uncertain, "uncertain", Severity::Medium),
            (Quality::Estimated, estimated, self.max_estimated, "estimated", Severity::Medium),
        ] {
            if s <= limit {
                continue;
            }
            let runs = runs_where(n, |i| f.quality[i] == q);
            for (i, (rs, re)) in runs.iter().enumerate() {
                if i >= self.max_run_findings {
                    break;
                }
                let w = run_window(&f, *rs, *re, interval);
                out.findings.push(Finding::new(
                    ID, &f.meta.id, Dimension::Validity, sev, w,
                    (re - rs) as f64 / n as f64,
                    format!("{} samples flagged {label} by the source ({:.1}% of the window, limit {:.1}%)",
                        re - rs, s * 100.0, limit * 100.0),
                    serde_json::json!({"quality": label, "run_samples": re - rs, "share": s, "limit": limit, "runs": runs.len()}),
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
    fn flags_bad_share() {
        let mut f = base(1000);
        inject::quality(&mut f, 100, 20, Quality::Bad);
        let out = QualityFlags::default().run(&f, &ctx(&f)).unwrap();
        let fs = ids(&out, ID);
        assert_eq!(fs.len(), 1);
        assert_eq!(fs[0].evidence["quality"], "bad");
    }

    #[test]
    fn estimated_within_limit_is_metric_only() {
        let mut f = base(1000);
        inject::quality(&mut f, 100, 50, Quality::Estimated);
        let out = QualityFlags::default().run(&f, &ctx(&f)).unwrap();
        assert!(ids(&out, ID).is_empty());
        assert!(out.metrics.iter().any(|m| m.name == "estimated_share" && (m.value - 0.05).abs() < 1e-9));
    }
}
