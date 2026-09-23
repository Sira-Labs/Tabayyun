//! [`SeriesFrame`]: one time series plus metadata, with Arrow conversion.

use crate::error::{Error, Result};
use crate::quality::Quality;
use crate::time::snap_to_nice;
use arrow::array::{
    Array, ArrayRef, Float32Array, Float64Array, Int32Array, Int64Array, RecordBatch, StringArray,
    TimestampMicrosecondArray, TimestampMillisecondArray, TimestampNanosecondArray, TimestampSecondArray,
    UInt16Array, UInt8Array,
};
use arrow::datatypes::{DataType, Field, Schema, TimeUnit};
use serde::{Deserialize, Serialize};
use std::sync::Arc;

/// What the series represents; some checks are skipped for setpoints and status tags.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum SeriesKind {
    #[default]
    Measurement,
    Counter,
    Setpoint,
    Status,
}

/// Series metadata. Everything optional except `id`; checks fall back to the baseline
/// [`crate::Profile`] when a field is missing.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
pub struct SeriesMeta {
    pub id: String,
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub unit: Option<String>,
    #[serde(default)]
    pub kind: SeriesKind,
    /// Expected sampling interval in ns. `None` = derive from data.
    #[serde(default)]
    pub expected_interval_ns: Option<i64>,
    #[serde(default)]
    pub physical_min: Option<f64>,
    #[serde(default)]
    pub physical_max: Option<f64>,
    /// Smallest meaningful change; `None` = derive from data.
    #[serde(default)]
    pub resolution: Option<f64>,
    /// Explicit non-negativity. `None` = infer from unit.
    #[serde(default)]
    pub non_negative: Option<bool>,
}

impl SeriesMeta {
    pub fn new(id: impl Into<String>) -> Self {
        Self { id: id.into(), ..Default::default() }
    }

    /// Non-negativity: explicit flag, else inferred from the unit.
    pub fn is_non_negative(&self) -> bool {
        if let Some(v) = self.non_negative {
            return v;
        }
        match self.unit.as_deref().map(|u| u.trim().to_ascii_lowercase()) {
            Some(u) => NON_NEGATIVE_UNITS.contains(&u.as_str()),
            None => false,
        }
    }

    /// Physical limits: explicit, else unit defaults (percent, kelvin, relative humidity...).
    pub fn physical_limits(&self) -> (Option<f64>, Option<f64>) {
        if self.physical_min.is_some() || self.physical_max.is_some() {
            return (self.physical_min, self.physical_max);
        }
        match self.unit.as_deref().map(|u| u.trim().to_ascii_lowercase()) {
            Some(u) => match u.as_str() {
                "%" | "percent" | "pct" => (Some(0.0), Some(100.0)),
                "%rh" | "rh" => (Some(0.0), Some(100.0)),
                "k" | "kelvin" => (Some(0.0), None),
                "ph" => (Some(0.0), Some(14.0)),
                "w/m2" | "w/m²" | "wm-2" => (Some(-4.0), Some(2000.0)),
                _ => (None, None),
            },
            None => (None, None),
        }
    }
}

/// Units whose physical quantity cannot be negative. Energy import and power are handled per
/// series (bidirectional meters exist), so they are *not* listed here.
const NON_NEGATIVE_UNITS: &[&str] = &[
    "m3/h", "m³/h", "m3/s", "l/s", "l/min", "kg/h", "t/h", "kg/s", "nm3/h", "sm3/h", "ppm", "ppb", "mg/l",
    "g/l", "mol/l", "%rh", "rh", "k", "kelvin", "kwh", "mwh", "wh", "m", "mm", "cm", "bar", "bara", "kpa",
    "pa", "psia", "rpm", "hz", "lux",
];

/// One time series. Timestamps are ns since epoch (UTC), values `f64` with NaN for null.
/// The frame keeps the order in which the source delivered samples; call
/// [`SeriesFrame::normalized`] to get a sorted, de-duplicated view for value checks.
#[derive(Debug, Clone, PartialEq)]
pub struct SeriesFrame {
    pub meta: SeriesMeta,
    pub ts: Vec<i64>,
    pub values: Vec<f64>,
    pub quality: Vec<Quality>,
    /// When each sample reached the historian / Tabayyun (ns since epoch), if known.
    /// Enables the latency check.
    pub ingest_ts: Option<Vec<i64>>,
}

/// What [`SeriesFrame::normalized`] changed.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Normalization {
    pub was_sorted: bool,
    pub exact_duplicates_removed: usize,
    pub conflicting_duplicates: usize,
}

impl SeriesFrame {
    pub fn new(meta: SeriesMeta, ts: Vec<i64>, values: Vec<f64>, quality: Vec<Quality>) -> Result<Self> {
        if ts.len() != values.len() || ts.len() != quality.len() {
            return Err(Error::InvalidFrame(format!(
                "length mismatch: ts={} values={} quality={}",
                ts.len(),
                values.len(),
                quality.len()
            )));
        }
        Ok(Self { meta, ts, values, quality, ingest_ts: None })
    }

    /// Attach ingest timestamps (same length as `ts`).
    pub fn with_ingest_ts(mut self, ingest_ts: Vec<i64>) -> Result<Self> {
        if ingest_ts.len() != self.ts.len() {
            return Err(Error::InvalidFrame(format!(
                "ingest_ts length {} != ts length {}",
                ingest_ts.len(),
                self.ts.len()
            )));
        }
        self.ingest_ts = Some(ingest_ts);
        Ok(self)
    }

    pub fn with_default_quality(meta: SeriesMeta, ts: Vec<i64>, values: Vec<f64>) -> Result<Self> {
        let q = vec![Quality::Good; ts.len()];
        Self::new(meta, ts, values, q)
    }

    pub fn len(&self) -> usize {
        self.ts.len()
    }

    pub fn is_empty(&self) -> bool {
        self.ts.is_empty()
    }

    pub fn is_sorted(&self) -> bool {
        self.ts.windows(2).all(|w| w[0] <= w[1])
    }

    pub fn first_ts(&self) -> Option<i64> {
        self.ts.iter().copied().min()
    }

    pub fn last_ts(&self) -> Option<i64> {
        self.ts.iter().copied().max()
    }

    /// Newest timestamp among samples whose quality is usable and whose value is finite.
    pub fn last_good_ts(&self) -> Option<i64> {
        self.ts
            .iter()
            .zip(&self.values)
            .zip(&self.quality)
            .filter(|((_, v), q)| q.is_usable() && v.is_finite())
            .map(|((t, _), _)| *t)
            .max()
    }

    /// Stable sort by timestamp and removal of exact duplicates (same ts, same value, same
    /// quality). Conflicting duplicates (same ts, different value) are kept (first wins for
    /// value checks) and counted so `timestamp_integrity` can report them.
    pub fn normalized(&self) -> (SeriesFrame, Normalization) {
        let mut idx: Vec<usize> = (0..self.len()).collect();
        let was_sorted = self.is_sorted();
        idx.sort_by_key(|&i| self.ts[i]);
        let mut ts = Vec::with_capacity(self.len());
        let mut values = Vec::with_capacity(self.len());
        let mut quality = Vec::with_capacity(self.len());
        let mut ingest: Vec<i64> = Vec::with_capacity(if self.ingest_ts.is_some() { self.len() } else { 0 });
        let mut report = Normalization { was_sorted, ..Default::default() };
        for &i in &idx {
            if let Some(&last_ts) = ts.last() {
                if last_ts == self.ts[i] {
                    let last_v: f64 = *values.last().unwrap();
                    let same_value = (last_v.is_nan() && self.values[i].is_nan()) || last_v == self.values[i];
                    if same_value && *quality.last().unwrap() == self.quality[i] {
                        report.exact_duplicates_removed += 1;
                    } else {
                        report.conflicting_duplicates += 1;
                    }
                    continue;
                }
            }
            ts.push(self.ts[i]);
            values.push(self.values[i]);
            quality.push(self.quality[i]);
            if let Some(ing) = &self.ingest_ts {
                ingest.push(ing[i]);
            }
        }
        let ingest_ts = self.ingest_ts.as_ref().map(|_| ingest);
        (SeriesFrame { meta: self.meta.clone(), ts, values, quality, ingest_ts }, report)
    }

    /// Expected interval: metadata first, else the snapped mode of inter-arrival times.
    pub fn expected_interval_ns(&self) -> Option<i64> {
        if let Some(v) = self.meta.expected_interval_ns {
            return Some(v);
        }
        modal_interval(&self.ts)
    }

    // ---- Arrow conversion -------------------------------------------------------------

    /// Build from a RecordBatch with a timestamp column, a numeric value column and an
    /// optional quality column (`UInt8`/`UInt16`/`Int32` code 0..3, or `Utf8` flag text).
    pub fn from_record_batch(
        meta: SeriesMeta,
        batch: &RecordBatch,
        ts_col: &str,
        value_col: &str,
        quality_col: Option<&str>,
    ) -> Result<Self> {
        Self::from_record_batch_ext(meta, batch, ts_col, value_col, quality_col, None)
    }

    /// As [`SeriesFrame::from_record_batch`], plus an optional ingest-timestamp column.
    pub fn from_record_batch_ext(
        meta: SeriesMeta,
        batch: &RecordBatch,
        ts_col: &str,
        value_col: &str,
        quality_col: Option<&str>,
        ingest_col: Option<&str>,
    ) -> Result<Self> {
        let ts = timestamps_from_column(column(batch, ts_col)?)?;
        let values = values_from_column(column(batch, value_col)?)?;
        let quality = match quality_col {
            Some(c) => quality_from_column(column(batch, c)?)?,
            None => vec![Quality::Good; ts.len()],
        };
        let frame = Self::new(meta, ts, values, quality)?;
        match ingest_col {
            Some(c) => frame.with_ingest_ts(timestamps_from_column(column(batch, c)?)?),
            None => Ok(frame),
        }
    }

    /// Standard batch: `ts: Timestamp(ns, UTC)`, `value: Float64`, `quality: UInt8`, plus
    /// `ingest_ts: Timestamp(ns, UTC)` when the frame carries ingest times.
    pub fn to_record_batch(&self) -> Result<RecordBatch> {
        let ts_type = DataType::Timestamp(TimeUnit::Nanosecond, Some("UTC".into()));
        let mut fields = vec![
            Field::new("ts", ts_type.clone(), false),
            Field::new("value", DataType::Float64, true),
            Field::new("quality", DataType::UInt8, false),
        ];
        let ts = TimestampNanosecondArray::from(self.ts.clone()).with_timezone("UTC");
        let values = Float64Array::from(
            self.values.iter().map(|v| if v.is_nan() { None } else { Some(*v) }).collect::<Vec<_>>(),
        );
        let quality = UInt8Array::from(self.quality.iter().map(|q| q.as_u8()).collect::<Vec<_>>());
        let mut columns: Vec<ArrayRef> = vec![Arc::new(ts), Arc::new(values), Arc::new(quality)];
        if let Some(ingest) = &self.ingest_ts {
            fields.push(Field::new("ingest_ts", ts_type, false));
            columns.push(Arc::new(TimestampNanosecondArray::from(ingest.clone()).with_timezone("UTC")));
        }
        Ok(RecordBatch::try_new(Arc::new(Schema::new(fields)), columns)?)
    }
}

fn column<'a>(batch: &'a RecordBatch, name: &str) -> Result<&'a ArrayRef> {
    batch.column_by_name(name).ok_or_else(|| Error::InvalidFrame(format!("column `{name}` not found")))
}

fn timestamps_from_column(col: &ArrayRef) -> Result<Vec<i64>> {
    macro_rules! conv {
        ($arr:ty, $mult:expr) => {{
            let a = col.as_any().downcast_ref::<$arr>().unwrap();
            if a.null_count() > 0 {
                return Err(Error::InvalidFrame("timestamp column contains nulls".into()));
            }
            Ok(a.values().iter().map(|v| v * $mult).collect())
        }};
    }
    match col.data_type() {
        DataType::Timestamp(TimeUnit::Nanosecond, _) => conv!(TimestampNanosecondArray, 1),
        DataType::Timestamp(TimeUnit::Microsecond, _) => conv!(TimestampMicrosecondArray, 1_000),
        DataType::Timestamp(TimeUnit::Millisecond, _) => conv!(TimestampMillisecondArray, 1_000_000),
        DataType::Timestamp(TimeUnit::Second, _) => conv!(TimestampSecondArray, 1_000_000_000),
        DataType::Int64 => conv!(Int64Array, 1),
        other => Err(Error::InvalidFrame(format!("unsupported timestamp type {other}"))),
    }
}

fn values_from_column(col: &ArrayRef) -> Result<Vec<f64>> {
    macro_rules! conv {
        ($arr:ty) => {{
            let a = col.as_any().downcast_ref::<$arr>().unwrap();
            Ok((0..a.len()).map(|i| if a.is_null(i) { f64::NAN } else { a.value(i) as f64 }).collect())
        }};
    }
    match col.data_type() {
        DataType::Float64 => conv!(Float64Array),
        DataType::Float32 => conv!(Float32Array),
        DataType::Int64 => conv!(Int64Array),
        DataType::Int32 => conv!(Int32Array),
        other => Err(Error::InvalidFrame(format!("unsupported value type {other}"))),
    }
}

fn quality_from_column(col: &ArrayRef) -> Result<Vec<Quality>> {
    macro_rules! conv_num {
        ($arr:ty) => {{
            let a = col.as_any().downcast_ref::<$arr>().unwrap();
            Ok((0..a.len())
                .map(|i| if a.is_null(i) { Quality::Good } else { Quality::from_u8(a.value(i) as u8) })
                .collect())
        }};
    }
    match col.data_type() {
        DataType::UInt8 => conv_num!(UInt8Array),
        DataType::UInt16 => conv_num!(UInt16Array),
        DataType::Int32 => conv_num!(Int32Array),
        DataType::Int64 => conv_num!(Int64Array),
        DataType::Utf8 => {
            let a = col.as_any().downcast_ref::<StringArray>().unwrap();
            Ok((0..a.len())
                .map(|i| {
                    if a.is_null(i) {
                        Quality::Good
                    } else {
                        Quality::parse(a.value(i)).unwrap_or(Quality::Bad)
                    }
                })
                .collect())
        }
        other => Err(Error::InvalidFrame(format!("unsupported quality type {other}"))),
    }
}

/// Mode of positive inter-arrival times, snapped to a nice interval. Requires ≥ 3 samples.
pub fn modal_interval(ts: &[i64]) -> Option<i64> {
    if ts.len() < 3 {
        return None;
    }
    let mut sorted = ts.to_vec();
    sorted.sort_unstable();
    let mut iats: Vec<i64> = sorted.windows(2).map(|w| w[1] - w[0]).filter(|d| *d > 0).collect();
    if iats.is_empty() {
        return None;
    }
    iats.sort_unstable();
    // Mode with 1 % tolerance bucketing, so jittered sources still yield one interval.
    let mut best = (iats[0], 0usize);
    let mut i = 0;
    while i < iats.len() {
        let anchor = iats[i];
        let tol = (anchor as f64 * 0.01).max(1.0) as i64;
        let mut j = i;
        while j < iats.len() && iats[j] - anchor <= tol {
            j += 1;
        }
        let count = j - i;
        if count > best.1 {
            best = (iats[i + count / 2], count);
        }
        i = j;
    }
    Some(snap_to_nice(best.0))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::time::NS_PER_MIN;

    #[test]
    fn normalization_sorts_and_dedupes() {
        let meta = SeriesMeta::new("t");
        let f = SeriesFrame::with_default_quality(meta, vec![3, 1, 2, 2, 2], vec![3.0, 1.0, 2.0, 2.0, 9.0])
            .unwrap();
        let (n, r) = f.normalized();
        assert_eq!(n.ts, vec![1, 2, 3]);
        assert_eq!(n.values, vec![1.0, 2.0, 3.0]);
        assert!(!r.was_sorted);
        assert_eq!(r.exact_duplicates_removed, 1);
        assert_eq!(r.conflicting_duplicates, 1);
    }

    #[test]
    fn modal_interval_snaps() {
        let ts: Vec<i64> = (0..100).map(|i| i * NS_PER_MIN + (i % 3) * 200_000_000).collect();
        assert_eq!(modal_interval(&ts), Some(NS_PER_MIN));
    }

    #[test]
    fn arrow_round_trip() {
        let meta = SeriesMeta::new("t");
        let f = SeriesFrame::new(
            meta.clone(),
            vec![1, 2, 3],
            vec![1.0, f64::NAN, 3.0],
            vec![Quality::Good, Quality::Bad, Quality::Estimated],
        )
        .unwrap();
        let b = f.to_record_batch().unwrap();
        let g = SeriesFrame::from_record_batch(meta, &b, "ts", "value", Some("quality")).unwrap();
        assert_eq!(g.ts, f.ts);
        assert!(g.values[1].is_nan());
        assert_eq!(g.quality, f.quality);
    }

    #[test]
    fn unit_inference() {
        let mut m = SeriesMeta::new("x");
        m.unit = Some("%".into());
        assert_eq!(m.physical_limits(), (Some(0.0), Some(100.0)));
        m.unit = Some("m3/h".into());
        assert!(m.is_non_negative());
        m.non_negative = Some(false);
        assert!(!m.is_non_negative());
    }
}
