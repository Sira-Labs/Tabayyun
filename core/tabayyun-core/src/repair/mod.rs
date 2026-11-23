//! Repair operations (ADR-0010, spec 034): each takes a raw frame and a window and returns a
//! new frame plus the [`Lineage`] of what it changed. The raw frame is only read.
//!
//! An operation touches samples inside the half-open window only; everything outside comes
//! back as it was. A sample is a hole when its value is not finite or its quality is not
//! usable (bad or uncertain); the imputing operations fill holes and missing timestamps and
//! mark what they fill as estimated.

mod fill;
mod lineage;
mod ops;

pub use lineage::{Action, Lineage};

use crate::error::{Error, Result};
use crate::finding::Window;
use crate::frame::SeriesFrame;
use crate::quality::Quality;
use serde::{Deserialize, Serialize};

/// Most missing timestamps an imputer estimates in one operation. A stray far timestamp
/// would otherwise ask it for a sample every second across years. Stretches in which nothing
/// can be estimated are not counted: they are skipped as a whole.
pub const MAX_ADDED: usize = 5_000_000;

/// Which of several samples that share a timestamp `dedupe` keeps.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum Keep {
    /// The first in the order of the frame.
    #[default]
    First,
    /// The last in the order of the frame.
    Last,
    /// One sample with the mean of the usable, finite values.
    Mean,
}

fn default_days() -> usize {
    4
}

/// A repair operation with its parameters. In JSON the method is the tag:
/// `{"method": "clamp", "min": 0.0}`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "method")]
pub enum Op {
    /// Every sample in the window becomes a null of bad quality.
    #[serde(rename = "mask")]
    Mask,
    /// Values below `min` become `min`, values above `max` become `max`, as estimated. A
    /// bound that is not given is the series' physical limit.
    #[serde(rename = "clamp")]
    Clamp {
        #[serde(default)]
        min: Option<f64>,
        #[serde(default)]
        max: Option<f64>,
    },
    /// Of the samples that share a timestamp, one stays.
    #[serde(rename = "dedupe")]
    Dedupe {
        #[serde(default)]
        keep: Keep,
    },
    /// Holes and missing timestamps get the value on the line between the usable samples
    /// around them. With `max_gap_ns`, only when those two are at most that far apart.
    #[serde(rename = "impute.linear")]
    ImputeLinear {
        #[serde(default)]
        max_gap_ns: Option<i64>,
    },
    /// Holes and missing timestamps get the mean of the usable samples at the same time of
    /// day on up to `days` days before and after (the same weekday with `same_weekday`).
    #[serde(rename = "impute.like_day")]
    ImputeLikeDay {
        #[serde(default = "default_days")]
        days: usize,
        #[serde(default)]
        same_weekday: bool,
    },
    /// The whole bins of `grid_ns` inside the window become one sample each, at the start of
    /// the bin, with the mean of the bin's usable, finite samples.
    #[serde(rename = "align.resample")]
    AlignResample { grid_ns: i64 },
    /// Every finite value `v` becomes `scale · v + offset`; qualities stay.
    #[serde(rename = "transform.affine")]
    TransformAffine {
        #[serde(default = "one")]
        scale: f64,
        #[serde(default)]
        offset: f64,
    },
}

fn one() -> f64 {
    1.0
}

impl Op {
    /// The method's name, as corrections store it.
    pub fn method(&self) -> &'static str {
        match self {
            Op::Mask => "mask",
            Op::Clamp { .. } => "clamp",
            Op::Dedupe { .. } => "dedupe",
            Op::ImputeLinear { .. } => "impute.linear",
            Op::ImputeLikeDay { .. } => "impute.like_day",
            Op::AlignResample { .. } => "align.resample",
            Op::TransformAffine { .. } => "transform.affine",
        }
    }
}

/// The names of all methods, in the order of ADR-0010.
pub const METHODS: [&str; 7] =
    ["mask", "clamp", "dedupe", "impute.linear", "impute.like_day", "align.resample", "transform.affine"];

/// The result of a repair.
#[derive(Debug, Clone, PartialEq)]
pub struct Repaired {
    /// The method that ran.
    pub method: &'static str,
    /// The repaired series: the raw samples outside the window, the operation's inside.
    pub frame: SeriesFrame,
    /// Every sample that differs from the raw frame.
    pub lineage: Lineage,
    /// Holes and missing timestamps an imputing operation had to leave as they were: no
    /// usable neighbour, a gap above `max_gap_ns`, or no reference day.
    pub unrepaired: usize,
}

/// The samples an operation puts in place of the window's, and how many it could not repair.
#[derive(Debug, Default)]
pub(crate) struct Patch {
    pub ts: Vec<i64>,
    pub values: Vec<f64>,
    pub quality: Vec<Quality>,
    pub unrepaired: usize,
}

impl Patch {
    pub(crate) fn push(&mut self, ts: i64, value: f64, quality: Quality) {
        self.ts.push(ts);
        self.values.push(value);
        self.quality.push(quality);
    }
}

pub(crate) fn invalid(reason: impl Into<String>) -> Error {
    Error::InvalidParams { check: "repair".into(), reason: reason.into() }
}

/// Whether a sample counts as a measurement an imputer may build on.
pub(crate) fn usable(value: f64, quality: Quality) -> bool {
    value.is_finite() && quality.is_usable()
}

/// Apply `op` to the samples of `raw` inside `window`.
///
/// `raw` must be sorted by time (samples that share a timestamp keep their order) and is not
/// changed. The repaired frame carries no ingest times.
///
/// Errors: an unsorted frame, a window that does not end after its start, parameters the
/// operation cannot use, or an imputer that would estimate more than [`MAX_ADDED`] missing
/// timestamps.
pub fn repair(raw: &SeriesFrame, window: Window, op: &Op) -> Result<Repaired> {
    if !raw.is_sorted() {
        return Err(invalid("the frame must be sorted by time"));
    }
    if window.end <= window.start {
        return Err(invalid("the window must end after it starts"));
    }
    let lo = raw.ts.partition_point(|&t| t < window.start);
    let hi = raw.ts.partition_point(|&t| t < window.end);
    let patch = match op {
        Op::Mask => ops::mask(raw, lo, hi),
        Op::Clamp { min, max } => ops::clamp(raw, lo, hi, *min, *max)?,
        Op::Dedupe { keep } => ops::dedupe(raw, lo, hi, *keep),
        Op::TransformAffine { scale, offset } => ops::affine(raw, lo, hi, *scale, *offset)?,
        Op::AlignResample { grid_ns } => ops::resample(raw, lo, hi, window, *grid_ns)?,
        Op::ImputeLinear { max_gap_ns } => fill::linear(raw, lo, hi, window, *max_gap_ns)?,
        Op::ImputeLikeDay { days, same_weekday } => {
            fill::like_day(raw, lo, hi, window, *days, *same_weekday)?
        }
    };
    let join = |before: &[i64], mid: &[i64], after: &[i64]| [before, mid, after].concat();
    let ts = join(&raw.ts[..lo], &patch.ts, &raw.ts[hi..]);
    let values = [&raw.values[..lo], &patch.values[..], &raw.values[hi..]].concat();
    let quality = [&raw.quality[..lo], &patch.quality[..], &raw.quality[hi..]].concat();
    let frame = SeriesFrame::new(raw.meta.clone(), ts, values, quality)?;
    let lineage = Lineage::diff(raw, &frame);
    Ok(Repaired { method: op.method(), frame, lineage, unrepaired: patch.unrepaired })
}

#[cfg(test)]
mod tests;
