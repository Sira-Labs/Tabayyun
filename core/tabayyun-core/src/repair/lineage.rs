//! The lineage of a repair: one row per sample that was changed, added or removed.
//!
//! A lineage is the difference between the raw frame and the repaired one, and it is enough
//! to get the raw samples back from the repaired frame ([`Lineage::revert`]). Corrections
//! store it as their point-level record (ADR-0010).

use crate::error::{Error, Result};
use crate::frame::SeriesFrame;
use crate::quality::Quality;
use arrow::array::{ArrayRef, Float64Array, RecordBatch, StringArray, TimestampNanosecondArray, UInt8Array};
use arrow::datatypes::{DataType, Field, Schema, TimeUnit};
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, VecDeque};
use std::sync::Arc;

/// What happened to a sample.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Action {
    /// The sample is still there with another value or quality.
    Changed,
    /// The repaired frame has a sample the raw frame did not have.
    Added,
    /// The raw frame had a sample the repaired frame does not have.
    Removed,
}

impl Action {
    pub fn as_str(self) -> &'static str {
        match self {
            Action::Changed => "changed",
            Action::Added => "added",
            Action::Removed => "removed",
        }
    }
}

/// Rows in time order. `before` and `quality_before` describe the raw sample (absent for an
/// added one), `after` and `quality_after` the repaired sample (absent for a removed one). A
/// value that is absent is NaN and its quality `None`; a NaN with a quality is a null sample.
#[derive(Debug, Clone, Default)]
pub struct Lineage {
    pub ts: Vec<i64>,
    pub action: Vec<Action>,
    pub before: Vec<f64>,
    pub after: Vec<f64>,
    pub quality_before: Vec<Option<Quality>>,
    pub quality_after: Vec<Option<Quality>>,
}

/// Two lineages are equal when their rows are; an absent value equals an absent value, which
/// the NaN that stands for it would not.
/// A run of timestamps a repair touched, with no untouched sample among them.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct LineageRange {
    pub start_ns: i64,
    /// The last touched timestamp plus one.
    pub end_ns: i64,
    pub changed: usize,
    pub added: usize,
    pub removed: usize,
}

impl PartialEq for Lineage {
    fn eq(&self, other: &Self) -> bool {
        let values = |a: &[f64], b: &[f64]| {
            a.len() == b.len() && a.iter().zip(b).all(|(x, y)| x == y || (x.is_nan() && y.is_nan()))
        };
        self.ts == other.ts
            && self.action == other.action
            && self.quality_before == other.quality_before
            && self.quality_after == other.quality_after
            && values(&self.before, &other.before)
            && values(&self.after, &other.after)
    }
}

type Sample = (f64, Quality);

/// Two samples are the same when their qualities are and their values are equal or both NaN.
fn same(a: Sample, b: Sample) -> bool {
    a.1 == b.1 && (a.0 == b.0 || (a.0.is_nan() && b.0.is_nan()))
}

/// The samples of one timestamp, from which matches are taken one at a time.
///
/// A match is the earliest sample not taken yet that is the same as the one asked for. Small
/// groups are searched; a large one (thousands of samples on one timestamp) is indexed, so
/// taking all of them stays linear.
struct Pool {
    samples: Vec<Option<Sample>>,
    index: Option<HashMap<(u64, u8), VecDeque<usize>>>,
}

impl Pool {
    /// Above this many samples the pool is indexed.
    const SEARCHED: usize = 16;

    /// Equal for exactly the samples [`same`] calls the same: every NaN is one value, and
    /// the two zeros are.
    fn key(sample: Sample) -> (u64, u8) {
        let value = if sample.0.is_nan() { u64::MAX } else { (sample.0 + 0.0).to_bits() };
        (value, sample.1.as_u8())
    }

    fn new(samples: impl Iterator<Item = Sample>) -> Self {
        let samples: Vec<Option<Sample>> = samples.map(Some).collect();
        let index = (samples.len() > Self::SEARCHED).then(|| {
            let mut index: HashMap<(u64, u8), VecDeque<usize>> = HashMap::new();
            for (i, s) in samples.iter().enumerate() {
                index.entry(Self::key(s.expect("just filled"))).or_default().push_back(i);
            }
            index
        });
        Self { samples, index }
    }

    /// Take the earliest sample left that is the same as `wanted`; false when there is none.
    fn take(&mut self, wanted: Sample) -> bool {
        let at = match &mut self.index {
            Some(index) => index.get_mut(&Self::key(wanted)).and_then(VecDeque::pop_front),
            None => self.samples.iter().position(|s| s.is_some_and(|s| same(s, wanted))),
        };
        at.map(|i| self.samples[i] = None).is_some()
    }

    /// The samples not taken, in their order.
    fn left(self) -> impl Iterator<Item = Sample> {
        self.samples.into_iter().flatten()
    }
}

fn invalid(reason: impl Into<String>) -> Error {
    Error::InvalidParams { check: "repair".into(), reason: reason.into() }
}

/// End of the run of equal timestamps that starts at `i`.
fn group_end(ts: &[i64], i: usize) -> usize {
    i + ts[i..].iter().take_while(|t| **t == ts[i]).count()
}

impl Lineage {
    pub fn len(&self) -> usize {
        self.ts.len()
    }

    pub fn is_empty(&self) -> bool {
        self.ts.is_empty()
    }

    /// How many rows have `action`.
    pub fn count(&self, action: Action) -> usize {
        self.action.iter().filter(|a| **a == action).count()
    }

    fn push(&mut self, ts: i64, before: Option<Sample>, after: Option<Sample>) {
        self.ts.push(ts);
        self.action.push(match (before, after) {
            (Some(_), Some(_)) => Action::Changed,
            (None, _) => Action::Added,
            (_, None) => Action::Removed,
        });
        self.before.push(before.map_or(f64::NAN, |s| s.0));
        self.after.push(after.map_or(f64::NAN, |s| s.0));
        self.quality_before.push(before.map(|s| s.1));
        self.quality_after.push(after.map(|s| s.1));
    }

    /// The lineage as runs of touched timestamps (spec 035). `frame_ts` are the repaired
    /// frame's timestamps, sorted: two consecutive lineage timestamps are in one range unless
    /// one of them lies strictly between, which is a sample the repair left alone. More than
    /// `max` ranges are joined with their neighbours, evenly, so that at most `max` remain.
    pub fn ranges(&self, frame_ts: &[i64], max: usize) -> Vec<LineageRange> {
        let mut ranges: Vec<LineageRange> = Vec::new();
        for (i, &t) in self.ts.iter().enumerate() {
            let separated = i > 0 && {
                let previous = self.ts[i - 1];
                let next = frame_ts.partition_point(|&u| u <= previous);
                frame_ts.get(next).is_some_and(|&u| u < t)
            };
            if i == 0 || separated {
                ranges.push(LineageRange { start_ns: t, end_ns: t, changed: 0, added: 0, removed: 0 });
            }
            let range = ranges.last_mut().expect("a range was pushed for the first row");
            range.end_ns = t.saturating_add(1);
            match self.action[i] {
                Action::Changed => range.changed += 1,
                Action::Added => range.added += 1,
                Action::Removed => range.removed += 1,
            }
        }
        let max = max.max(1);
        if ranges.len() <= max {
            return ranges;
        }
        let per = ranges.len().div_ceil(max);
        ranges
            .chunks(per)
            .map(|chunk| LineageRange {
                start_ns: chunk[0].start_ns,
                end_ns: chunk[chunk.len() - 1].end_ns,
                changed: chunk.iter().map(|r| r.changed).sum(),
                added: chunk.iter().map(|r| r.added).sum(),
                removed: chunk.iter().map(|r| r.removed).sum(),
            })
            .collect()
    }

    /// The difference between two frames that are sorted by time.
    ///
    /// Samples are compared timestamp by timestamp. Where several share a timestamp, those
    /// that are the same on both sides are matched first; what is left is paired in order as
    /// changed, and the surplus of either side is removed or added.
    pub fn diff(raw: &SeriesFrame, repaired: &SeriesFrame) -> Lineage {
        let mut out = Lineage::default();
        let (mut i, mut j) = (0, 0);
        while i < raw.len() || j < repaired.len() {
            let t = match (raw.ts.get(i), repaired.ts.get(j)) {
                (Some(a), Some(b)) => *a.min(b),
                (Some(a), None) => *a,
                (None, Some(b)) => *b,
                (None, None) => break,
            };
            let i_end = if raw.ts.get(i) == Some(&t) { group_end(&raw.ts, i) } else { i };
            let j_end = if repaired.ts.get(j) == Some(&t) { group_end(&repaired.ts, j) } else { j };
            let mut after = Pool::new((j..j_end).map(|k| (repaired.values[k], repaired.quality[k])));
            let left: Vec<Sample> =
                (i..i_end).map(|k| (raw.values[k], raw.quality[k])).filter(|b| !after.take(*b)).collect();
            let mut rest = after.left();
            for b in left {
                out.push(t, Some(b), rest.next());
            }
            for a in rest {
                out.push(t, None, Some(a));
            }
            (i, j) = (i_end, j_end);
        }
        out
    }

    /// The raw samples, from the repaired frame this lineage belongs to.
    ///
    /// The result is sorted by time. Samples that share a timestamp come back in no
    /// particular order among themselves, and ingest times are not restored.
    pub fn revert(&self, repaired: &SeriesFrame) -> Result<SeriesFrame> {
        if !repaired.is_sorted() || !self.ts.windows(2).all(|w| w[0] <= w[1]) {
            return Err(invalid("the frame and the lineage must be sorted by time"));
        }
        let n = repaired.len() + self.count(Action::Removed);
        let (mut ts, mut values, mut quality) =
            (Vec::with_capacity(n), Vec::with_capacity(n), Vec::with_capacity(n));
        let (mut j, mut r) = (0, 0);
        while j < repaired.len() || r < self.len() {
            let t = match (repaired.ts.get(j), self.ts.get(r)) {
                (Some(a), Some(b)) => *a.min(b),
                (Some(a), None) => *a,
                (None, Some(b)) => *b,
                (None, None) => break,
            };
            let j_end = if repaired.ts.get(j) == Some(&t) { group_end(&repaired.ts, j) } else { j };
            let r_end = if self.ts.get(r) == Some(&t) { group_end(&self.ts, r) } else { r };
            let mut kept = Pool::new((j..j_end).map(|k| (repaired.values[k], repaired.quality[k])));
            let mut back = Vec::new();
            for k in r..r_end {
                if let Some(q) = self.quality_after[k] {
                    if !kept.take((self.after[k], q)) {
                        return Err(invalid(format!("the frame has no sample for the lineage row at {t}")));
                    }
                }
                if let Some(q) = self.quality_before[k] {
                    back.push((self.before[k], q));
                }
            }
            for (v, q) in kept.left().chain(back) {
                ts.push(t);
                values.push(v);
                quality.push(q);
            }
            (j, r) = (j_end, r_end);
        }
        SeriesFrame::new(repaired.meta.clone(), ts, values, quality)
    }

    /// The rows as a record batch: `ts: Timestamp(ns, UTC)`, `action: Utf8`, `before` and
    /// `after: Float64` (null for NaN and for an absent sample), `quality_before` and
    /// `quality_after: UInt8` (null for an absent sample).
    pub fn to_record_batch(&self) -> Result<RecordBatch> {
        let value = |v: &[f64]| -> ArrayRef {
            Arc::new(Float64Array::from(v.iter().map(|x| (!x.is_nan()).then_some(*x)).collect::<Vec<_>>()))
        };
        let quality = |q: &[Option<Quality>]| -> ArrayRef {
            Arc::new(UInt8Array::from(q.iter().map(|x| x.map(Quality::as_u8)).collect::<Vec<_>>()))
        };
        let fields = vec![
            Field::new("ts", DataType::Timestamp(TimeUnit::Nanosecond, Some("UTC".into())), false),
            Field::new("action", DataType::Utf8, false),
            Field::new("before", DataType::Float64, true),
            Field::new("after", DataType::Float64, true),
            Field::new("quality_before", DataType::UInt8, true),
            Field::new("quality_after", DataType::UInt8, true),
        ];
        let columns: Vec<ArrayRef> = vec![
            Arc::new(TimestampNanosecondArray::from(self.ts.clone()).with_timezone("UTC")),
            Arc::new(StringArray::from(self.action.iter().map(|a| a.as_str()).collect::<Vec<_>>())),
            value(&self.before),
            value(&self.after),
            quality(&self.quality_before),
            quality(&self.quality_after),
        ];
        Ok(RecordBatch::try_new(Arc::new(Schema::new(fields)), columns)?)
    }
}
