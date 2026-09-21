//! The [`Check`] trait and the built-in checks.
//!
//! Every check is pure and idempotent: `run(frame, ctx) -> CheckOutput`. Checks that need
//! metadata the frame does not carry return [`Error::MissingMetadata`] so the caller can
//! report a skip instead of a false pass.

pub mod completeness;
pub mod flatline;
pub mod interpolation_artifacts;
pub mod non_negative;
pub mod operational_range;
pub mod physical_range;
pub mod quality_flags;
pub mod rate_of_change;
pub mod resolution_loss;
pub mod sampling_regularity;
pub mod spikes;
pub mod staleness;
pub mod timestamp_integrity;
pub mod value_type;

use crate::error::Result;
use crate::finding::{Dimension, Finding, Metric, Severity, Window};
use crate::frame::SeriesFrame;
use crate::profile::Profile;

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
        let end = frame.last_ts().unwrap_or(0) + 1;
        Self { now_ns: end - 1, window: Window::new(start, end), profile: None }
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
    let e = if end < frame.len() { frame.ts[end] } else { frame.ts[end - 1] + interval_ns.max(1) };
    Window::new(s, e.max(s + 1))
}

/// Helper used by several checks: expected interval from metadata, profile, or data.
pub(crate) fn expected_interval(frame: &SeriesFrame, ctx: &CheckContext) -> Option<i64> {
    frame
        .meta
        .expected_interval_ns
        .or_else(|| ctx.profile.as_ref().and_then(|p| p.expected_interval_ns))
        .or_else(|| frame.expected_interval_ns())
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
}
