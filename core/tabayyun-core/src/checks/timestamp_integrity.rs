//! `tby.timestamp_integrity` — duplicate and out-of-order timestamps (catalogue #4).

use super::{metric, Check, CheckContext, CheckOutput};
use crate::error::Result;
use crate::finding::{Dimension, Finding, Severity};
use crate::frame::SeriesFrame;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

pub const ID: &str = "tby.timestamp_integrity";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct TimestampIntegrity {
    /// Exact duplicates (same ts, value and quality) are tolerated and only reported as a metric.
    pub allow_exact_duplicates: bool,
    /// Values closer than this at the same timestamp count as exact duplicates.
    pub conflict_tolerance: f64,
    pub max_examples: usize,
    pub severity: Severity,
}

impl Default for TimestampIntegrity {
    fn default() -> Self {
        Self {
            allow_exact_duplicates: true,
            conflict_tolerance: 0.0,
            max_examples: 20,
            severity: Severity::High,
        }
    }
}

impl Check for TimestampIntegrity {
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
        let n = frame.len();
        if n == 0 {
            return Ok(out);
        }
        // Out-of-order: count descents in delivery order.
        let mut descents = 0usize;
        let mut descent_examples = Vec::new();
        for w in frame.ts.windows(2) {
            if w[1] < w[0] {
                descents += 1;
                if descent_examples.len() < self.max_examples {
                    descent_examples.push(w[1]);
                }
            }
        }
        // Duplicates by timestamp.
        let mut seen: HashMap<i64, usize> = HashMap::with_capacity(n);
        let mut exact = 0usize;
        let mut conflicts = 0usize;
        let mut conflict_examples = Vec::new();
        for i in 0..n {
            match seen.get(&frame.ts[i]) {
                None => {
                    seen.insert(frame.ts[i], i);
                }
                Some(&j) => {
                    let a = frame.values[j];
                    let b = frame.values[i];
                    let same = (a.is_nan() && b.is_nan()) || (a - b).abs() <= self.conflict_tolerance;
                    if same && frame.quality[j] == frame.quality[i] {
                        exact += 1;
                    } else {
                        conflicts += 1;
                        if conflict_examples.len() < self.max_examples {
                            conflict_examples.push(serde_json::json!({"ts": frame.ts[i], "values": [a, b]}));
                        }
                    }
                }
            }
        }
        out.metrics.push(metric(ID, frame, "exact_duplicates", ctx.window.end, exact as f64));
        out.metrics.push(metric(ID, frame, "conflicting_duplicates", ctx.window.end, conflicts as f64));
        out.metrics.push(metric(ID, frame, "out_of_order", ctx.window.end, descents as f64));

        let w = ctx.window;
        if conflicts > 0 {
            out.findings.push(Finding::new(
                ID,
                &frame.meta.id,
                Dimension::Integrity,
                self.severity,
                w,
                conflicts as f64 / n as f64,
                format!("{conflicts} timestamps carry conflicting values (same time, different value)"),
                serde_json::json!({"conflicting_duplicates": conflicts, "examples": conflict_examples}),
            ));
        }
        if exact > 0 && !self.allow_exact_duplicates {
            out.findings.push(Finding::new(
                ID,
                &frame.meta.id,
                Dimension::Integrity,
                Severity::Low,
                w,
                exact as f64 / n as f64,
                format!("{exact} exact duplicate samples"),
                serde_json::json!({"exact_duplicates": exact}),
            ));
        }
        if descents > 0 {
            out.findings.push(Finding::new(
                ID,
                &frame.meta.id,
                Dimension::Integrity,
                Severity::Medium,
                w,
                descents as f64 / n as f64,
                format!("{descents} samples arrived out of chronological order"),
                serde_json::json!({"out_of_order": descents, "examples": descent_examples}),
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
    fn clean_passes() {
        let f = base(100);
        assert!(ids(&TimestampIntegrity::default().run(&f, &ctx(&f)).unwrap(), ID).is_empty());
    }

    #[test]
    fn exact_duplicates_only_metric_by_default() {
        let mut f = base(100);
        inject::duplicate(&mut f, 10, 3);
        let out = TimestampIntegrity::default().run(&f, &ctx(&f)).unwrap();
        // The appended duplicates also create out-of-order descents (appended at the end).
        assert!(ids(&out, ID).iter().all(|x| x.evidence.get("out_of_order").is_some()));
        assert!(out.metrics.iter().any(|m| m.name == "exact_duplicates" && m.value == 3.0));
    }

    #[test]
    fn conflicts_and_order() {
        let mut f = base(100);
        inject::conflicting_duplicate(&mut f, 5, 1.0);
        inject::swap(&mut f, 20, 21);
        let out = TimestampIntegrity::default().run(&f, &ctx(&f)).unwrap();
        let fs = ids(&out, ID);
        assert!(fs.iter().any(|x| x.evidence["conflicting_duplicates"] == 1));
        assert!(fs.iter().any(|x| x.evidence["out_of_order"].as_u64().is_some_and(|n| n >= 1)));
    }
}
