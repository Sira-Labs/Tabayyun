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
            let before: Vec<Sample> = (i..i_end).map(|k| (raw.values[k], raw.quality[k])).collect();
            let mut after: Vec<Option<Sample>> =
                (j..j_end).map(|k| Some((repaired.values[k], repaired.quality[k]))).collect();
            let mut left = Vec::new();
            for b in before {
                match after.iter_mut().find(|a| a.is_some_and(|a| same(a, b))) {
                    Some(matched) => *matched = None,
                    None => left.push(b),
                }
            }
            let mut rest = after.into_iter().flatten();
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
            let mut kept: Vec<Option<Sample>> =
                (j..j_end).map(|k| Some((repaired.values[k], repaired.quality[k]))).collect();
            let mut back = Vec::new();
            for k in r..r_end {
                if let Some(q) = self.quality_after[k] {
                    let found = kept.iter_mut().find(|s| s.is_some_and(|s| same(s, (self.after[k], q))));
                    match found {
                        Some(slot) => *slot = None,
                        None => {
                            return Err(invalid(format!(
                                "the frame has no sample for the lineage row at {t}"
                            )))
                        }
                    }
                }
                if let Some(q) = self.quality_before[k] {
                    back.push((self.before[k], q));
                }
            }
            for (v, q) in kept.into_iter().flatten().chain(back) {
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
