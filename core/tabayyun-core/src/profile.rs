//! Baseline profile: robust per-series statistics used as adaptive thresholds.
//!
//! Sprint 1 computes only what the structural checks need; the full profile (seasonality,
//! autocorrelation, noise, quantiles of rate of change) arrives with the drift checks.

use crate::frame::{modal_interval, SeriesFrame};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
pub struct Profile {
    pub series_id: String,
    pub n_samples: usize,
    pub n_finite: usize,
    /// Snapped mode of inter-arrival times, ns.
    pub expected_interval_ns: Option<i64>,
    pub iat_p50_ns: Option<i64>,
    pub iat_p99_ns: Option<i64>,
    pub median: Option<f64>,
    /// Median absolute deviation (unscaled).
    pub mad: Option<f64>,
    pub p01: Option<f64>,
    pub p99: Option<f64>,
    pub min: Option<f64>,
    pub max: Option<f64>,
    /// Smallest non-zero |Δvalue| between consecutive samples.
    pub resolution: Option<f64>,
    pub distinct_values: usize,
    /// Share of samples inside runs of ≥ 10 identical values.
    pub constant_fraction: f64,
    pub quality_good: f64,
    pub quality_uncertain: f64,
    pub quality_bad: f64,
    pub quality_estimated: f64,
}

impl Profile {
    pub fn compute(frame: &SeriesFrame) -> Self {
        let (f, _) = frame.normalized();
        let n = f.len();
        let mut p = Profile { series_id: f.meta.id.clone(), n_samples: n, ..Default::default() };
        if n == 0 {
            return p;
        }
        p.expected_interval_ns = modal_interval(&f.ts);
        let mut iats: Vec<i64> = f.ts.windows(2).map(|w| w[1] - w[0]).collect();
        if !iats.is_empty() {
            iats.sort_unstable();
            p.iat_p50_ns = Some(quantile_i64(&iats, 0.5));
            p.iat_p99_ns = Some(quantile_i64(&iats, 0.99));
        }
        let mut finite: Vec<f64> = f.values.iter().copied().filter(|v| v.is_finite()).collect();
        p.n_finite = finite.len();
        if !finite.is_empty() {
            finite.sort_by(|a, b| a.partial_cmp(b).unwrap());
            p.min = finite.first().copied();
            p.max = finite.last().copied();
            let med = quantile_f64(&finite, 0.5);
            p.median = Some(med);
            p.p01 = Some(quantile_f64(&finite, 0.01));
            p.p99 = Some(quantile_f64(&finite, 0.99));
            let mut dev: Vec<f64> = finite.iter().map(|v| (v - med).abs()).collect();
            dev.sort_by(|a, b| a.partial_cmp(b).unwrap());
            p.mad = Some(quantile_f64(&dev, 0.5));
            p.distinct_values = count_distinct_sorted(&finite);
        }
        p.resolution = resolution(&f.values);
        p.constant_fraction = constant_fraction(&f.values, 10);
        let total = n as f64;
        for q in &f.quality {
            match q {
                crate::Quality::Good => p.quality_good += 1.0 / total,
                crate::Quality::Uncertain => p.quality_uncertain += 1.0 / total,
                crate::Quality::Bad => p.quality_bad += 1.0 / total,
                crate::Quality::Estimated => p.quality_estimated += 1.0 / total,
            }
        }
        p
    }
}

fn quantile_i64(sorted: &[i64], q: f64) -> i64 {
    let idx = ((sorted.len() - 1) as f64 * q).round() as usize;
    sorted[idx.min(sorted.len() - 1)]
}

pub(crate) fn quantile_f64(sorted: &[f64], q: f64) -> f64 {
    let idx = ((sorted.len() - 1) as f64 * q).round() as usize;
    sorted[idx.min(sorted.len() - 1)]
}

fn count_distinct_sorted(sorted: &[f64]) -> usize {
    if sorted.is_empty() {
        return 0;
    }
    1 + sorted.windows(2).filter(|w| w[0] != w[1]).count()
}

/// Smallest non-zero absolute difference between consecutive finite values.
pub fn resolution(values: &[f64]) -> Option<f64> {
    let mut best: Option<f64> = None;
    let mut prev: Option<f64> = None;
    for &v in values {
        if !v.is_finite() {
            continue;
        }
        if let Some(p) = prev {
            let d = (v - p).abs();
            if d > 0.0 && best.is_none_or(|b| d < b) {
                best = Some(d);
            }
        }
        prev = Some(v);
    }
    best
}

/// Fraction of samples that sit inside runs of at least `min_run` identical finite values.
pub fn constant_fraction(values: &[f64], min_run: usize) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    let mut in_runs = 0usize;
    let mut run_start = 0usize;
    for i in 1..=values.len() {
        let continues = i < values.len() && values[i].is_finite() && values[i] == values[run_start];
        if !continues {
            let len = i - run_start;
            if len >= min_run && values[run_start].is_finite() {
                in_runs += len;
            }
            run_start = i;
        }
    }
    in_runs as f64 / values.len() as f64
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::frame::SeriesMeta;
    use crate::time::NS_PER_MIN;

    #[test]
    fn profile_basic() {
        let ts: Vec<i64> = (0..60).map(|i| i * NS_PER_MIN).collect();
        let mut values: Vec<f64> = (0..60).map(|i| (i % 7) as f64).collect();
        for v in values.iter_mut().take(30).skip(10) {
            *v = 3.0;
        }
        let f = SeriesFrame::with_default_quality(SeriesMeta::new("s"), ts, values).unwrap();
        let p = Profile::compute(&f);
        assert_eq!(p.expected_interval_ns, Some(NS_PER_MIN));
        assert_eq!(p.resolution, Some(1.0));
        assert_eq!(p.distinct_values, 7);
        assert!((p.constant_fraction - 20.0 / 60.0).abs() < 1e-9);
        assert!((p.quality_good - 1.0).abs() < 1e-9);
    }
}
