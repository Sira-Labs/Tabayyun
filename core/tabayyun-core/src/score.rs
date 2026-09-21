//! Scoring v2 (see `docs/architecture/02-domain-model.md`).
//!
//! Within a dimension, span findings are merged on the time axis. Each is spread evenly
//! over its window with density `score_impact × window_duration / finding_duration`, so a
//! finding that covers the whole window but affects one sample in ten thousand stays
//! negligible, while a gap keeps its full severity weight. The impact is the integral of
//! the *maximum* density among findings covering each instant, divided by the window
//! duration: two checks flagging the same minute count once (at the higher density).
//! Point findings (windows too narrow to carry their impact at the severity weight, such
//! as spikes) add their own `score_impact`; identical point windows count once.
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
    /// Score with the evaluation window used to normalise merged coverage.
    ///
    /// Span findings are spread over their (clipped) window with density
    /// `score_impact × window_duration / finding_duration` and merged by maximum, so the
    /// affected fraction survives: a whole-window finding that touched one sample in ten
    /// thousand contributes almost nothing, a gap keeps its full weight. A finding whose
    /// window is too narrow to carry its impact at the severity weight (a point finding
    /// such as a spike) keeps its `score_impact` additively; identical point windows are
    /// deduplicated by maximum impact.
    pub fn score_window(
        &self,
        series_id: &str,
        findings: &[Finding],
        window: crate::finding::Window,
    ) -> ScoreReport {
        let mut impact: BTreeMap<Dimension, f64> = BTreeMap::new();
        let span = window.duration().max(1) as f64;
        for d in Dimension::ALL {
            let mut spans: Vec<(i64, i64, f64)> = Vec::new();
            let mut points: BTreeMap<(i64, i64), f64> = BTreeMap::new();
            for f in findings.iter().filter(|f| f.series_id == series_id && f.dimension == d) {
                if f.window.end <= window.start || f.window.start >= window.end {
                    continue;
                }
                let s = f.window.start.max(window.start);
                let e = f.window.end.min(window.end).max(s + 1);
                let density = f.score_impact * span / (e - s) as f64;
                if density > f.severity.weight() {
                    let slot = points.entry((s, e)).or_insert(0.0);
                    *slot = slot.max(f.score_impact);
                } else {
                    spans.push((s, e, density));
                }
            }
            if spans.is_empty() && points.is_empty() {
                continue;
            }
            let merged = merged_coverage(&spans, window.start, window.end);
            let additive: f64 = points.values().sum();
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

    fn finding(id: &str, d: Dimension, sev: Severity, w: Window, frac: f64) -> Finding {
        Finding::new(id, "s", d, sev, w, frac, "x", serde_json::json!({}))
    }

    #[test]
    fn overlapping_findings_count_once_at_max_density() {
        let w = Window::new(0, 1_000_000_000);
        let a = finding("tby.a", Dimension::Validity, Severity::Critical, Window::new(0, 500_000_000), 0.5);
        let b = finding("tby.b", Dimension::Validity, Severity::High, Window::new(0, 500_000_000), 0.5);
        let r = Scorer::default().score_window("s", &[a.clone(), b], w);
        // Half the window at density 1.0 → impact 0.5 → score 50, not 0.
        assert_eq!(r.dimensions[&Dimension::Validity], 50.0);
        assert_eq!(r.dimensions[&Dimension::Completeness], 100.0);
        assert_eq!(r.n_findings, 2);
        assert_eq!(r.method_version, "v2");
        assert_eq!(Scorer::default().score_window("s", &[a], w).dimensions[&Dimension::Validity], 50.0);
    }

    #[test]
    fn whole_window_finding_with_tiny_fraction_stays_negligible() {
        let w = Window::new(0, 1_000_000_000);
        // One conflicting timestamp in 10,000 samples: High severity, affected 1/10000.
        let f = finding("tby.timestamp_integrity", Dimension::Integrity, Severity::High, w, 1.0 / 10_000.0);
        let r = Scorer::default().score_window("s", &[f], w);
        assert!(r.dimensions[&Dimension::Integrity] > 99.9, "{:?}", r.dimensions);
        // A gap covering half the window at High keeps its full weight: 100 − 60 × 0.5 = 70.
        let g = finding(
            "tby.completeness",
            Dimension::Completeness,
            Severity::High,
            Window::new(0, 500_000_000),
            0.5,
        );
        let r = Scorer::default().score_window("s", &[g], w);
        assert_eq!(r.dimensions[&Dimension::Completeness], 70.0);
        // Point findings (spikes) add up: three spikes of impact 0.3/1000 each.
        let spikes: Vec<Finding> = (0..3)
            .map(|i| {
                finding(
                    "tby.spikes",
                    Dimension::Plausibility,
                    Severity::Medium,
                    Window::new(i * 1000, i * 1000 + 1),
                    1.0 / 1000.0,
                )
            })
            .collect();
        let r = Scorer::default().score_window("s", &spikes, w);
        // 3 × 0.3 / 1000 = 0.0009 impact → 99.91 → 99.9 after rounding; the one-nanosecond
        // windows must not lose their impact to the density cap.
        assert_eq!(r.dimensions[&Dimension::Plausibility], 99.9);
        // Identical point windows count once (max), not three times: 0.3 × 0.1 = 0.03 → 97.0.
        let same: Vec<Finding> = (0..3)
            .map(|_| finding("tby.spikes", Dimension::Plausibility, Severity::Medium, Window::new(0, 1), 0.1))
            .collect();
        let r = Scorer::default().score_window("s", &same, w);
        assert_eq!(r.dimensions[&Dimension::Plausibility], 97.0);
        // Attenuated findings (changepoint uses overlap × 0.5) keep their attenuation.
        let c = finding("tby.changepoint", Dimension::Plausibility, Severity::Medium, w, 0.5);
        let r = Scorer::default().score_window("s", &[c], w);
        assert_eq!(r.dimensions[&Dimension::Plausibility], 85.0);
    }

    #[test]
    fn merged_coverage_handles_nesting() {
        let iv = [(0, 100, 0.3), (10, 20, 1.0), (50, 150, 0.6)];
        let c = merged_coverage(&iv, 0, 100);
        // 0-10 @0.3 = 3, 10-20 @1.0 = 10, 20-50 @0.3 = 9, 50-100 @0.6 = 30 → 52/100
        assert!((c - 0.52).abs() < 1e-9, "{c}");
    }
}
