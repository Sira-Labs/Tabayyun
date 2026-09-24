//! The [`Check`] trait and the built-in checks.
//!
//! Every check is pure and idempotent: `run(frame, ctx) -> CheckOutput`. Checks that need
//! metadata the frame does not carry return [`Error::MissingMetadata`] so the caller can
//! report a skip instead of a false pass.

pub mod balance_residual;
pub mod changepoint;
pub mod completeness;
pub mod correlation_break;
pub mod distribution_drift;
pub mod flatline;
pub mod interpolation_artifacts;
pub mod latency;
pub mod level_drift;
pub mod noise_level;
pub mod non_negative;
pub mod operational_range;
pub mod physical_range;
pub mod quality_flags;
pub mod rate_of_change;
pub mod redundant_disagreement;
pub mod resolution_loss;
pub mod sampling_regularity;
pub mod scale_shift;
pub mod seasonality_break;
pub mod spikes;
pub mod staleness;
pub mod timestamp_integrity;
pub mod value_type;

use crate::error::{Error, Result};
use crate::finding::{Dimension, Finding, Metric, Severity, Window};
use crate::frame::SeriesFrame;
use crate::profile::Profile;
use crate::time::parse_duration;
use std::borrow::Cow;

/// Evaluation context shared by all checks in a run.
#[derive(Debug, Clone)]
pub struct CheckContext {
    /// "Now" for staleness and latency, ns since epoch.
    pub now_ns: i64,
    /// Evaluation window. Affected fractions are relative to this window.
    pub window: Window,
    /// Baseline profile, if one was computed (adaptive thresholds).
    pub profile: Option<Profile>,
}

impl CheckContext {
    /// Window spanning the frame's own extent, "now" = last timestamp. Convenient for
    /// batch evaluation of historical files.
    pub fn from_frame(frame: &SeriesFrame) -> Self {
        let start = frame.first_ts().unwrap_or(0);
        let last = frame.last_ts().unwrap_or(0);
        Self { now_ns: last, window: Window::new(start, last.saturating_add(1)), profile: None }
    }

    pub fn with_profile(mut self, profile: Profile) -> Self {
        self.profile = Some(profile);
        self
    }

    pub fn with_now(mut self, now_ns: i64) -> Self {
        self.now_ns = now_ns;
        self
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct CheckOutput {
    pub findings: Vec<Finding>,
    pub metrics: Vec<Metric>,
    /// `(check_id, missing metadata field)` for checks that could not run.
    pub skipped: Vec<(String, String)>,
}

impl CheckOutput {
    pub fn extend(&mut self, other: CheckOutput) {
        self.findings.extend(other.findings);
        self.metrics.extend(other.metrics);
        self.skipped.extend(other.skipped);
    }
}

pub trait Check: Send + Sync {
    fn id(&self) -> &'static str;
    fn dimension(&self) -> Dimension;
    fn default_severity(&self) -> Severity;
    fn run(&self, frame: &SeriesFrame, ctx: &CheckContext) -> Result<CheckOutput>;
}

/// Contiguous index runs `[start, end)` where `pred(i)` holds.
pub(crate) fn runs_where(n: usize, mut pred: impl FnMut(usize) -> bool) -> Vec<(usize, usize)> {
    let mut runs = Vec::new();
    let mut start: Option<usize> = None;
    for i in 0..n {
        match (pred(i), start) {
            (true, None) => start = Some(i),
            (false, Some(s)) => {
                runs.push((s, i));
                start = None;
            }
            _ => {}
        }
    }
    if let Some(s) = start {
        runs.push((s, n));
    }
    runs
}

/// Window covered by index run `[start, end)` of a sorted frame. The end is the next
/// sample's timestamp (or the last sample + one expected interval when the run reaches the
/// end of the frame).
pub(crate) fn run_window(frame: &SeriesFrame, start: usize, end: usize, interval_ns: i64) -> Window {
    let s = frame.ts[start];
    let e =
        if end < frame.len() { frame.ts[end] } else { frame.ts[end - 1].saturating_add(interval_ns.max(1)) };
    Window::new(s, e.max(s.saturating_add(1)))
}

/// Helper used by several checks: expected interval from metadata, profile, or data.
pub(crate) fn expected_interval(frame: &SeriesFrame, ctx: &CheckContext) -> Option<i64> {
    frame
        .meta
        .expected_interval_ns
        .or_else(|| ctx.profile.as_ref().and_then(|p| p.expected_interval_ns))
        .or_else(|| frame.expected_interval_ns())
}

/// Split a sorted frame into consecutive time segments of `segment_ns` (aligned to the first
/// timestamp). Yields `(start_idx, end_idx, window)` for non-empty segments.
pub(crate) fn segments(frame: &SeriesFrame, segment_ns: i64) -> Vec<(usize, usize, Window)> {
    let n = frame.len();
    let mut out = Vec::new();
    if n == 0 || segment_ns <= 0 {
        return out;
    }
    let start = frame.ts[0];
    let mut s = 0usize;
    while s < n {
        let k = (frame.ts[s].saturating_sub(start) / segment_ns).saturating_add(1);
        let seg_end_ts = start.saturating_add(k.saturating_mul(segment_ns));
        let mut e = s;
        while e < n && frame.ts[e] < seg_end_ts {
            e += 1;
        }
        let e = e.max(s + 1);
        out.push((s, e, Window::new(frame.ts[s], frame.ts[e - 1].saturating_add(1))));
        s = e;
    }
    out
}

/// Merge flagged segments into episodes: items whose segment indices are consecutive form one
/// group whose window spans them all. Segment checks report one finding per episode rather
/// than one per segment, so a shift that lasts a month is one finding, not thirty.
pub(crate) fn episodes<T>(mut flagged: Vec<(usize, Window, T)>) -> Vec<(Window, Vec<T>)> {
    flagged.sort_by_key(|(k, _, _)| *k);
    let mut out: Vec<(usize, Window, Vec<T>)> = Vec::new();
    for (k, w, item) in flagged {
        match out.last_mut() {
            Some((last_k, last_w, items)) if k == *last_k + 1 => {
                *last_k = k;
                last_w.end = last_w.end.max(w.end);
                items.push(item);
            }
            _ => out.push((k, w, vec![item])),
        }
    }
    out.into_iter().map(|(_, w, items)| (w, items)).collect()
}

/// Which of the per-segment statistics `xs` are unusual among the segments of the window:
/// more than `k` robust sigmas (1.4826 × MAD) above the median, or on either side when
/// `two_sided`. With fewer than `min_segments` values the siblings say nothing and every
/// segment counts as unusual, leaving the absolute thresholds alone to decide.
pub(crate) fn unusual_among(xs: &[f64], min_segments: usize, k: f64, two_sided: bool) -> Vec<bool> {
    let Some((med, mad)) = crate::profile::median_mad(xs).filter(|_| xs.len() >= min_segments.max(1)) else {
        return vec![true; xs.len()];
    };
    let limit = k * 1.4826 * mad;
    xs.iter().map(|&x| if two_sided { (x - med).abs() > limit } else { x - med > limit }).collect()
}

/// Baseline profile for adaptive thresholds: the run's profile when present, otherwise a
/// profile of the frame itself. The label says which one was used, for evidence.
pub(crate) fn baseline<'a>(ctx: &'a CheckContext, frame: &SeriesFrame) -> (Cow<'a, Profile>, &'static str) {
    match &ctx.profile {
        Some(p) => (Cow::Borrowed(p), "baseline"),
        None => (Cow::Owned(Profile::compute(frame)), "self"),
    }
}

/// Finite values with usable quality in index range `[s, e)`.
pub(crate) fn usable_values(frame: &SeriesFrame, s: usize, e: usize) -> Vec<f64> {
    (s..e)
        .filter(|&i| frame.values[i].is_finite() && frame.quality[i].is_usable())
        .map(|i| frame.values[i])
        .collect()
}

/// Timestamps and values of usable samples in `[s, e)`, for difference-based statistics.
pub(crate) fn usable_pairs(frame: &SeriesFrame, s: usize, e: usize) -> (Vec<i64>, Vec<f64>) {
    let idx: Vec<usize> =
        (s..e).filter(|&i| frame.values[i].is_finite() && frame.quality[i].is_usable()).collect();
    (idx.iter().map(|&i| frame.ts[i]).collect(), idx.iter().map(|&i| frame.values[i]).collect())
}

/// Parse a duration parameter, turning a malformed string into `Error::InvalidParams`.
pub(crate) fn duration_param(check: &str, name: &str, value: &str) -> Result<i64> {
    parse_duration(value).ok_or_else(|| Error::InvalidParams {
        check: check.to_string(),
        reason: format!("{name}: cannot parse duration `{value}` (use e.g. 90s, 15m, 2h, 1d)"),
    })
}

pub(crate) fn metric(check_id: &str, frame: &SeriesFrame, name: &str, ts: i64, value: f64) -> Metric {
    Metric { check_id: check_id.into(), series_id: frame.meta.id.clone(), name: name.into(), ts, value }
}

#[cfg(test)]
pub(crate) mod testutil {
    use super::*;
    use crate::synth::{generate, SynthSpec};

    pub fn base(n: usize) -> SeriesFrame {
        generate(&SynthSpec { n, ..SynthSpec::default() })
    }

    pub fn ctx(frame: &SeriesFrame) -> CheckContext {
        CheckContext::from_frame(frame)
    }

    pub fn ids<'a>(out: &'a CheckOutput, id: &str) -> Vec<&'a Finding> {
        out.findings.iter().filter(|f| f.check_id == id).collect()
    }

    #[test]
    fn unusual_among_flags_outliers_once_there_are_enough_segments() {
        let xs = [1.0, 1.1, 0.9, 1.0, 1.05, 0.95, 1.0, 4.0, 0.2];
        let u = unusual_among(&xs, 8, 3.0, false);
        assert_eq!(u, [false, false, false, false, false, false, false, true, false]);
        let u = unusual_among(&xs, 8, 3.0, true);
        assert_eq!(u, [false, false, false, false, false, false, false, true, true]);
        assert!(unusual_among(&xs[..4], 8, 3.0, false).iter().all(|b| *b));
    }

    #[test]
    fn episodes_merge_consecutive_segments_only() {
        let seg = |k: usize| (k, Window::new(k as i64 * 10, k as i64 * 10 + 9), k);
        let groups = episodes(vec![seg(5), seg(1), seg(2), seg(3), seg(7)]);
        assert_eq!(groups.len(), 3);
        assert_eq!(groups[0].0, Window::new(10, 39));
        assert_eq!(groups[0].1, vec![1, 2, 3]);
        assert_eq!(groups[1].1, vec![5]);
        assert_eq!(groups[2].1, vec![7]);
    }
}
