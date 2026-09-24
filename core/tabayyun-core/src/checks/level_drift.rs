//! `tby.level_drift` — slow bias / trend via Theil–Sen on segment medians (catalogue #18).

use super::{baseline, metric, segments, usable_values, Check, CheckContext, CheckOutput};
use crate::error::Result;
use crate::finding::{Dimension, Finding, Severity, Window};
use crate::frame::SeriesFrame;
use crate::profile::{median_mad, quantile_f64};
use crate::time::{format_duration, NS_PER_DAY};
use serde::{Deserialize, Serialize};

pub const ID: &str = "tby.level_drift";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct LevelDrift {
    /// Aggregation segment for medians (default one day).
    pub segment_ns: i64,
    /// Drift over this horizon is compared with the baseline spread.
    pub horizon_ns: i64,
    /// Finding when |slope × horizon| > k × reference, where reference is the larger of the
    /// baseline noise sigma and a tenth of the baseline spread (1.4826 × MAD), so a slow bias
    /// is judged against noise rather than against seasonal swing.
    pub k: f64,
    pub min_segments: usize,
    /// Upper bound on segments; the segment is widened beyond this so the O(n²) Theil–Sen
    /// estimator stays cheap on long series.
    pub max_segments: usize,
    pub severity: Severity,
}

impl Default for LevelDrift {
    fn default() -> Self {
        Self {
            segment_ns: NS_PER_DAY,
            horizon_ns: 30 * NS_PER_DAY,
            k: 3.0,
            min_segments: 7,
            max_segments: 400,
            severity: Severity::Medium,
        }
    }
}

/// Theil–Sen slope: median of pairwise slopes. O(n²), fine for ≤ a few thousand points.
pub fn theil_sen(x: &[f64], y: &[f64]) -> Option<f64> {
    let n = x.len().min(y.len());
    if n < 2 {
        return None;
    }
    let mut slopes = Vec::with_capacity(n * (n - 1) / 2);
    for i in 0..n {
        for j in (i + 1)..n {
            if x[j] != x[i] {
                slopes.push((y[j] - y[i]) / (x[j] - x[i]));
            }
        }
    }
    if slopes.is_empty() {
        return None;
    }
    slopes.sort_by(|a, b| a.partial_cmp(b).unwrap());
    Some(quantile_f64(&slopes, 0.5))
}

impl Check for LevelDrift {
    fn id(&self) -> &'static str {
        ID
    }
    fn dimension(&self) -> Dimension {
        Dimension::Accuracy
    }
    fn default_severity(&self) -> Severity {
        self.severity
    }

    fn run(&self, frame: &SeriesFrame, ctx: &CheckContext) -> Result<CheckOutput> {
        let mut out = CheckOutput::default();
        let (f, _) = frame.normalized();
        let (profile, source) = baseline(ctx, &f);
        let Some(mad) = profile.mad else { return Ok(out) };
        if f.is_empty() {
            return Ok(out);
        }
        let spread =
            profile.noise_mad.unwrap_or(0.0).max(0.1 * 1.4826 * mad).max(profile.resolution.unwrap_or(0.0));
        let span = f.ts[f.len() - 1].saturating_sub(f.ts[0]).max(1);
        let mut segment_ns = self.segment_ns.max(1);
        if span / segment_ns >= self.max_segments as i64 {
            segment_ns = (span as f64 / self.max_segments as f64).ceil() as i64;
        }
        let mut xs = Vec::new();
        let mut ys = Vec::new();
        for (s, e, w) in segments(&f, segment_ns) {
            let seg = usable_values(&f, s, e);
            if seg.len() < 10 {
                continue;
            }
            let Some((med, _)) = median_mad(&seg) else { continue };
            xs.push(w.start.saturating_sub(f.ts[0]) as f64 / NS_PER_DAY as f64);
            ys.push(med);
        }
        if xs.len() < self.min_segments {
            return Ok(out);
        }
        let Some(slope_per_day) = theil_sen(&xs, &ys) else { return Ok(out) };
        let drift = slope_per_day * (self.horizon_ns as f64 / NS_PER_DAY as f64);
        out.metrics.push(metric(ID, &f, "slope_per_day", ctx.window.end, slope_per_day));
        if spread > 0.0 && drift.abs() > self.k * spread {
            let w = Window::new(f.ts[0], f.ts[f.len() - 1].saturating_add(1));
            out.findings.push(Finding::new(
                ID, &f.meta.id, Dimension::Accuracy, self.severity, w,
                ctx.window.overlap_fraction(&w) * 0.5,
                format!("Level drifts by {slope_per_day:.4} per day ({drift:+.4} over {}, {:.1}× the noise reference)",
                    format_duration(self.horizon_ns), drift.abs() / spread),
                serde_json::json!({"slope_per_day": slope_per_day, "drift_over_horizon": drift, "horizon_ns": self.horizon_ns,
                    "reference": spread, "segments": xs.len(), "segment_ns": segment_ns, "baseline": source}),
            ));
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::checks::testutil::*;
    use crate::Profile;

    #[test]
    fn ramp_over_two_weeks_flagged() {
        let profile = Profile::compute(&base(14 * 1440));
        let mut f = base(14 * 1440);
        for (k, v) in f.values.iter_mut().enumerate() {
            *v += 0.01 * k as f64 / 1440.0 * 60.0; // +0.6 units per day → 18 over 30 days
        }
        let c = ctx(&f).with_profile(profile);
        let out = LevelDrift::default().run(&f, &c).unwrap();
        assert_eq!(ids(&out, ID).len(), 1, "{:?}", out.findings);
    }

    #[test]
    fn segment_count_is_capped() {
        let f = base(30 * 1440);
        let c = LevelDrift { segment_ns: 60 * 1_000_000_000, max_segments: 50, ..Default::default() };
        let out = c.run(&f, &ctx(&f)).unwrap();
        // No drift in the base signal, and the run must finish quickly with ≤ 50 segments.
        assert!(ids(&out, ID).is_empty());
    }

    #[test]
    fn empty_frame_with_baseline_does_not_panic() {
        let profile = Profile::compute(&base(14 * 1440));
        let f = base(0);
        let c = CheckContext { now_ns: 0, window: crate::finding::Window::new(0, 1), profile: Some(profile) };
        assert!(LevelDrift::default().run(&f, &c).unwrap().findings.is_empty());
    }

    #[test]
    fn stationary_passes() {
        let f = base(14 * 1440);
        assert!(ids(&LevelDrift::default().run(&f, &ctx(&f)).unwrap(), ID).is_empty());
    }
}
