//! `tby.sampling_regularity` — jitter and sampling-interval changes (catalogue #5).
//!
//! The DST "expected samples per local day" rule needs a timezone database and arrives
//! with the metering pack; this check covers regularity and interval changes.

use super::{expected_interval, metric, Check, CheckContext, CheckOutput};
use crate::error::Result;
use crate::finding::{Dimension, Finding, Severity, Window};
use crate::frame::{modal_interval, SeriesFrame};
use crate::time::{format_duration, NS_PER_DAY};
use serde::{Deserialize, Serialize};

pub const ID: &str = "tby.sampling_regularity";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct SamplingRegularity {
    /// Relative tolerance around the expected interval for an IAT to count as regular.
    pub jitter_tol: f64,
    /// Below this share of regular IATs a finding is raised.
    pub min_regularity: f64,
    /// Segment length (ns) used to detect a changed sampling interval.
    pub segment_ns: i64,
    /// A segment whose modal interval differs from the expected one by more than this
    /// relative amount is reported as an interval change.
    pub change_tol: f64,
    pub severity: Severity,
}

impl Default for SamplingRegularity {
    fn default() -> Self {
        Self {
            jitter_tol: 0.10,
            min_regularity: 0.90,
            segment_ns: NS_PER_DAY,
            change_tol: 0.25,
            severity: Severity::Medium,
        }
    }
}

impl Check for SamplingRegularity {
    fn id(&self) -> &'static str {
        ID
    }
    fn dimension(&self) -> Dimension {
        Dimension::Integrity
    }
    fn default_severity(&self) -> Severity {
        self.severity
    }

    fn run(&self, frame: &SeriesFrame, ctx: &CheckContext) -> Result<CheckOutput> {
        let mut out = CheckOutput::default();
        let (f, _) = frame.normalized();
        if f.len() < 3 {
            return Ok(out);
        }
        let Some(expected) = expected_interval(&f, ctx) else { return Ok(out) };
        let iats: Vec<i64> = crate::time::steps(&f.ts).collect();
        // Gaps (handled by completeness) are excluded from the regularity statistic.
        let gap_cut = expected.saturating_mul(3);
        let considered: Vec<i64> = iats.iter().copied().filter(|d| *d <= gap_cut).collect();
        let regular = considered
            .iter()
            .filter(|d| ((**d - expected).abs() as f64) <= self.jitter_tol * expected as f64)
            .count();
        let regularity = if considered.is_empty() { 1.0 } else { regular as f64 / considered.len() as f64 };
        out.metrics.push(metric(ID, &f, "regularity", ctx.window.end, regularity));

        if regularity < self.min_regularity {
            out.findings.push(Finding::new(
                ID, &f.meta.id, Dimension::Integrity, self.severity, ctx.window,
                (1.0 - regularity) * 0.5,
                format!("Only {:.1}% of samples arrive within ±{:.0}% of the expected {} interval",
                    regularity * 100.0, self.jitter_tol * 100.0, format_duration(expected)),
                serde_json::json!({"regularity": regularity, "expected_interval_ns": expected, "jitter_tol": self.jitter_tol}),
            ));
        }

        // Interval change: modal IAT per segment vs expected.
        let start = f.ts[0];
        let mut seg_start_idx = 0usize;
        let mut seg_no = 0i64;
        let mut changes: Vec<(Window, i64)> = Vec::new();
        for i in 0..=f.len() {
            let boundary =
                i == f.len() || f.ts[i].saturating_sub(start) >= (seg_no + 1).saturating_mul(self.segment_ns);
            if boundary {
                if i - seg_start_idx >= 8 {
                    if let Some(m) = modal_interval(&f.ts[seg_start_idx..i]) {
                        if ((m - expected).abs() as f64) > self.change_tol * expected as f64 {
                            let w = Window::new(
                                f.ts[seg_start_idx],
                                if i < f.len() { f.ts[i] } else { f.ts[i - 1].saturating_add(m) },
                            );
                            changes.push((w, m));
                        }
                    }
                }
                seg_start_idx = i;
                seg_no += 1;
                if i == f.len() {
                    break;
                }
            }
        }
        // Merge adjacent segments with the same new interval.
        let mut merged: Vec<(Window, i64)> = Vec::new();
        for (w, m) in changes {
            if let Some((lw, lm)) = merged.last_mut() {
                if *lm == m && lw.end == w.start {
                    lw.end = w.end;
                    continue;
                }
            }
            merged.push((w, m));
        }
        for (w, m) in merged {
            out.findings.push(Finding::new(
                ID,
                &f.meta.id,
                Dimension::Integrity,
                self.severity,
                w,
                ctx.window.overlap_fraction(&w) * 0.5,
                format!(
                    "Sampling interval changed from {} to {}",
                    format_duration(expected),
                    format_duration(m)
                ),
                serde_json::json!({"expected_interval_ns": expected, "observed_interval_ns": m}),
            ));
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::checks::testutil::*;
    use crate::synth::Rng;
    use crate::time::NS_PER_SEC;

    #[test]
    fn regular_passes() {
        let f = base(500);
        assert!(ids(&SamplingRegularity::default().run(&f, &ctx(&f)).unwrap(), ID).is_empty());
    }

    #[test]
    fn jitter_flagged() {
        let mut f = base(500);
        let mut rng = Rng::new(7);
        for t in f.ts.iter_mut() {
            *t += (rng.uniform() * 30.0 * NS_PER_SEC as f64) as i64; // up to 50 % jitter
        }
        let out = SamplingRegularity::default().run(&f, &ctx(&f)).unwrap();
        assert!(ids(&out, ID).iter().any(|x| x.evidence.get("regularity").is_some()));
    }

    #[test]
    fn interval_change_flagged() {
        let mut f = base(3 * 1440);
        // Third day: every 5 minutes instead of every minute → thin the last day.
        let cut = 2 * 1440;
        let keep: Vec<usize> = (0..f.len()).filter(|i| *i < cut || (*i - cut) % 5 == 0).collect();
        f.ts = keep.iter().map(|&i| f.ts[i]).collect();
        f.values = keep.iter().map(|&i| f.values[i]).collect();
        f.quality = keep.iter().map(|&i| f.quality[i]).collect();
        let out = SamplingRegularity::default().run(&f, &ctx(&f)).unwrap();
        let c: Vec<_> =
            ids(&out, ID).into_iter().filter(|x| x.evidence.get("observed_interval_ns").is_some()).collect();
        assert_eq!(c.len(), 1, "{:?}", out.findings);
        assert_eq!(c[0].evidence["observed_interval_ns"], serde_json::json!(5 * 60 * NS_PER_SEC));
    }
}
