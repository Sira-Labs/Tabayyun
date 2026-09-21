//! Scoring v2 (see `docs/architecture/02-domain-model.md`).
//!
//! Within a dimension, findings are merged on the time axis. Each finding is spread evenly
//! over its window with density `score_impact × window_duration / finding_duration`
//! (capped at its severity weight), so a finding that covers the whole window but affects
//! one sample in ten thousand stays negligible, while a gap keeps its full severity weight.
//! The impact is the integral of the *maximum* density among findings covering each
//! instant, divided by the window duration: two checks flagging the same minute count once
//! (at the higher density) instead of twice.
//! Series dimension score = 100 × (1 − clip(impact)); overall = weighted mean of dimensions.

use crate::finding::{Dimension, Finding};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

pub const METHOD_VERSION: &str = "v2";

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Scorer {
    /// Relative weights per dimension for the overall score.
    pub weights: BTreeMap<Dimension, f64>,
}

impl Default for Scorer {
    fn default() -> Self {
        let mut weights = BTreeMap::new();
        weights.insert(Dimension::Completeness, 1.0);
        weights.insert(Dimension::Timeliness, 1.0);
        weights.insert(Dimension::Validity, 1.5);
        weights.insert(Dimension::Accuracy, 1.0);
        weights.insert(Dimension::Consistency, 1.0);
        weights.insert(Dimension::Plausibility, 1.0);
        weights.insert(Dimension::Integrity, 1.0);
        Self { weights }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ScoreReport {
    pub series_id: String,
    pub method_version: String,
    pub overall: f64,
    pub dimensions: BTreeMap<Dimension, f64>,
    pub n_findings: usize,
}

/// Integral of the maximum weight over `[start, end)` among weighted intervals, divided by
/// the span duration. Intervals outside the span are clipped.
pub fn merged_coverage(intervals: &[(i64, i64, f64)], start: i64, end: i64) -> f64 {
    let span = (end - start).max(1) as f64;
    let mut events: Vec<(i64, bool, f64)> = Vec::with_capacity(intervals.len() * 2);
    for &(s, e, w) in intervals {
        let (s, e) = (s.max(start), e.min(end));
        if e > s && w > 0.0 {
            events.push((s, true, w));
            events.push((e, false, w));
        }
    }
    if events.is_empty() {
        return 0.0;
    }
    events.sort_by(|a, b| a.0.cmp(&b.0).then(a.1.cmp(&b.1)));
    // Active weights as a multiset (counts per weight); weights are few distinct values.
    let mut active: BTreeMap<u64, usize> = BTreeMap::new();
    let key = |w: f64| (w * 1e6).round() as u64;
    let mut integral = 0.0;
    let mut prev_t = events[0].0;
    for (t, open, w) in events {
        if let Some((&k, _)) = active.iter().next_back() {
            integral += (t - prev_t) as f64 * (k as f64 / 1e6);
        }
        prev_t = t;
        let c = active.entry(key(w)).or_insert(0);
        if open {
            *c += 1;
        } else if *c <= 1 {
            active.remove(&key(w));
        } else {
            *c -= 1;
        }
    }
    (integral / span).clamp(0.0, 1.0)
}

impl Scorer {
    /// Score with the evaluation window used to normalise merged coverage. When findings
    /// carry no usable span (all point findings) the additive impact is used.
    pub fn score_window(
        &self,
        series_id: &str,
        findings: &[Finding],
        window: crate::finding::Window,
    ) -> ScoreReport {
        let mut impact: BTreeMap<Dimension, f64> = BTreeMap::new();
        for d in Dimension::ALL {
            let mine: Vec<&Finding> =
                findings.iter().filter(|f| f.series_id == series_id && f.dimension == d).collect();
            if mine.is_empty() {
                continue;
            }
            // Point findings (spikes, single samples) are additive; spans are merged.
            let point_threshold = window.duration() / 10_000;
            let (points, spans): (Vec<&Finding>, Vec<&Finding>) =
                mine.iter().partition(|f| f.window.duration() <= point_threshold.max(1));
            let additive: f64 = points.iter().map(|f| f.score_impact).sum();
            let intervals: Vec<(i64, i64, f64)> =
                spans.iter().map(|f| (f.window.start, f.window.end, f.severity.weight())).collect();
            let merged = merged_coverage(&intervals, window.start, window.end);
            impact.insert(d, (merged + additive).clamp(0.0, 1.0));
        }
        self.finish(series_id, findings, impact)
    }

    /// Score using the union of finding windows as the evaluation window.
    pub fn score(&self, series_id: &str, findings: &[Finding]) -> ScoreReport {
        let mine = findings.iter().filter(|f| f.series_id == series_id);
        let start = mine.clone().map(|f| f.window.start).min().unwrap_or(0);
        let end = mine.map(|f| f.window.end).max().unwrap_or(1);
        self.score_window(series_id, findings, crate::finding::Window::new(start, end.max(start + 1)))
    }

    fn finish(&self, series_id: &str, findings: &[Finding], impact: BTreeMap<Dimension, f64>) -> ScoreReport {
        let mut dimensions = BTreeMap::new();
        let mut num = 0.0;
        let mut den = 0.0;
        for d in Dimension::ALL {
            let s = 100.0 * (1.0 - impact.get(&d).copied().unwrap_or(0.0).clamp(0.0, 1.0));
            let s = (s * 10.0).round() / 10.0;
            dimensions.insert(d, s);
            let w = self.weights.get(&d).copied().unwrap_or(1.0);
            num += w * s;
            den += w;
        }
        ScoreReport {
            series_id: series_id.to_string(),
            method_version: METHOD_VERSION.into(),
            overall: if den > 0.0 { ((num / den) * 10.0).round() / 10.0 } else { 100.0 },
            dimensions,
            n_findings: findings.iter().filter(|f| f.series_id == series_id).count(),
        }
    }
}

impl PartialOrd for Dimension {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for Dimension {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        (*self as u8).cmp(&(*other as u8))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::finding::{Severity, Window};

    #[test]
    fn scores_clip_and_weight() {
        let f = Finding::new(
            "tby.x",
            "s",
            Dimension::Validity,
            Severity::Critical,
            Window::new(0, 10),
            0.5,
            "x",
            serde_json::json!({}),
        );
        let r = Scorer::default().score("s", &[f.clone(), f]);
        assert_eq!(r.dimensions[&Dimension::Validity], 0.0);
        assert_eq!(r.dimensions[&Dimension::Completeness], 100.0);
        assert!(r.overall < 100.0 && r.overall > 70.0);
        assert_eq!(r.n_findings, 2);
    }
}
