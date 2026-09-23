//! Baseline profile: robust per-series statistics used as adaptive thresholds.
//!
//! Structural statistics, robust noise and value quantiles, and the dominant period with its
//! seasonal strength (`seasonal`, spec 012).

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
    pub p001: Option<f64>,
    pub p01: Option<f64>,
    pub p99: Option<f64>,
    pub p999: Option<f64>,
    pub min: Option<f64>,
    pub max: Option<f64>,
    /// Smallest non-zero |Δvalue| between consecutive samples.
    pub resolution: Option<f64>,
    pub distinct_values: usize,
    /// Share of samples inside runs of ≥ 10 identical values.
    pub constant_fraction: f64,
    /// 99.9th percentile of |Δvalue / Δt| in value units per second (gaps excluded).
    pub rate_p999: Option<f64>,
    /// Robust noise sigma: 1.4826 × MAD of signed first differences / √2, i.e. the standard
    /// deviation of additive noise if the signal were locally constant.
    pub noise_mad: Option<f64>,
    /// Share of samples inside perfectly linear runs of ≥ 6 samples (interpolation signature).
    pub linear_fraction: f64,
    /// Value quantiles at 0, 5, 10, ..., 100 % (21 points) for distribution comparisons.
    pub quantiles: Vec<f64>,
    pub quality_good: f64,
    pub quality_uncertain: f64,
    pub quality_bad: f64,
    pub quality_estimated: f64,
    /// Shortest candidate period (1 d, 7 d, 365 d) with a rhythm, ns (spec 012).
    pub dominant_period_ns: Option<i64>,
    /// Hyndman's seasonal strength F_S (0–1) at `dominant_period_ns`.
    pub seasonal_strength: Option<f64>,
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
            p.p001 = Some(quantile_f64(&finite, 0.001));
            p.p01 = Some(quantile_f64(&finite, 0.01));
            p.p99 = Some(quantile_f64(&finite, 0.99));
            p.p999 = Some(quantile_f64(&finite, 0.999));
            let mut dev: Vec<f64> = finite.iter().map(|v| (v - med).abs()).collect();
            dev.sort_by(|a, b| a.partial_cmp(b).unwrap());
            p.mad = Some(quantile_f64(&dev, 0.5));
            p.distinct_values = count_distinct_sorted(&finite);
            p.quantiles = (0..=20).map(|k| quantile_f64(&finite, k as f64 / 20.0)).collect();
        }
        p.resolution = resolution(&f.values);
        p.constant_fraction = constant_fraction(&f.values, 10);
        p.linear_fraction = linear_fraction(&f.values, 6, p.resolution.unwrap_or(0.0) * 0.5);
        let gap_cut = p.expected_interval_ns.map(|i| 3 * i).unwrap_or(i64::MAX);
        let mut rates: Vec<f64> = Vec::with_capacity(n);
        let mut diffs: Vec<f64> = Vec::with_capacity(n);
        for i in 1..n {
            let dt = f.ts[i] - f.ts[i - 1];
            let (a, b) = (f.values[i - 1], f.values[i]);
            if dt <= 0 || dt > gap_cut || !a.is_finite() || !b.is_finite() {
                continue;
            }
            diffs.push(b - a);
            rates.push((b - a).abs() / (dt as f64 / 1e9));
        }
        if !rates.is_empty() {
            rates.sort_by(|a, b| a.partial_cmp(b).unwrap());
            p.rate_p999 = Some(quantile_f64(&rates, 0.999));
            p.noise_mad = robust_sigma_of_diffs(&mut diffs);
        }
        if let Some((period, strength)) = crate::seasonal::detect(&f, &crate::seasonal::DEFAULT_CANDIDATES_NS)
        {
            p.dominant_period_ns = Some(period);
            p.seasonal_strength = Some(strength);
        }
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

/// Robust noise sigma of a series: 1.4826 × MAD of signed first differences / √2, skipping
/// pairs separated by more than `gap_cut_ns` or involving non-finite values.
pub fn noise_sigma(ts: &[i64], values: &[f64], gap_cut_ns: i64) -> Option<f64> {
    let mut diffs = Vec::with_capacity(values.len());
    for i in 1..values.len().min(ts.len()) {
        let dt = ts[i] - ts[i - 1];
        if dt <= 0 || dt > gap_cut_ns || !values[i].is_finite() || !values[i - 1].is_finite() {
            continue;
        }
        diffs.push(values[i] - values[i - 1]);
    }
    robust_sigma_of_diffs(&mut diffs)
}

fn robust_sigma_of_diffs(diffs: &mut [f64]) -> Option<f64> {
    if diffs.is_empty() {
        return None;
    }
    diffs.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let med = quantile_f64(diffs, 0.5);
    let mut dev: Vec<f64> = diffs.iter().map(|d| (d - med).abs()).collect();
    dev.sort_by(|a, b| a.partial_cmp(b).unwrap());
    Some(1.4826 * quantile_f64(&dev, 0.5) / std::f64::consts::SQRT_2)
}

fn quantile_i64(sorted: &[i64], q: f64) -> i64 {
    let idx = ((sorted.len() - 1) as f64 * q).round() as usize;
    sorted[idx.min(sorted.len() - 1)]
}

/// Median and unscaled MAD of a value set (sorts a copy). `None` for an empty set.
pub fn median_mad(values: &[f64]) -> Option<(f64, f64)> {
    let mut v: Vec<f64> = values.iter().copied().filter(|x| x.is_finite()).collect();
    if v.is_empty() {
        return None;
    }
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let med = quantile_f64(&v, 0.5);
    let mut dev: Vec<f64> = v.iter().map(|x| (x - med).abs()).collect();
    dev.sort_by(|a, b| a.partial_cmp(b).unwrap());
    Some((med, quantile_f64(&dev, 0.5)))
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

/// Contiguous runs `[start, end)` of ≥ `min_run` samples whose second difference is within
/// `atol` of zero and whose first difference is *not* zero (flat runs are not linear runs).
pub fn linear_runs(values: &[f64], min_run: usize, atol: f64) -> Vec<(usize, usize)> {
    let n = values.len();
    let mut runs = Vec::new();
    if n < 3 {
        return runs;
    }
    let mut start = 0usize;
    let mut i = 2;
    while i <= n {
        let ok = i < n && {
            let (a, b, c) = (values[i - 2], values[i - 1], values[i]);
            a.is_finite()
                && b.is_finite()
                && c.is_finite()
                && ((c - b) - (b - a)).abs() <= atol
                && (b - a).abs() > atol
        };
        if !ok {
            // A new candidate starts on the last sample of the previous one; clip it so runs
            // never overlap and the summed length never exceeds the series length.
            let s0 = runs.last().map_or(start, |&(_, prev_end)| start.max(prev_end));
            if i - s0 >= min_run && i >= 2 {
                let first_ok = values[s0].is_finite()
                    && values[s0 + 1].is_finite()
                    && (values[s0 + 1] - values[s0]).abs() > atol;
                if first_ok {
                    runs.push((s0, i));
                }
            }
            start = i - 1;
        }
        i += 1;
    }
    runs
}

/// Share of samples inside linear runs (see [`linear_runs`]).
pub fn linear_fraction(values: &[f64], min_run: usize, atol: f64) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    let total: usize = linear_runs(values, min_run, atol).iter().map(|(s, e)| e - s).sum();
    total as f64 / values.len() as f64
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

    /// Runs that meet at a corner sample must not both claim it.
    #[test]
    fn adjoining_linear_runs_do_not_overlap() {
        // Slope 1 for 20 samples, then slope 2 for 20 samples, then slope 3: three runs that
        // meet at shared corner samples. Their summed length must not exceed the series length.
        let mut v = Vec::new();
        let mut x = 0.0;
        for slope in [1.0, 2.0, 3.0] {
            for _ in 0..20 {
                v.push(x);
                x += slope;
            }
        }
        let runs = linear_runs(&v, 6, 1e-9);
        assert_eq!(runs.len(), 3, "{runs:?}");
        for w in runs.windows(2) {
            assert!(w[0].1 <= w[1].0, "overlap: {runs:?}");
        }
        let total: usize = runs.iter().map(|(s, e)| e - s).sum();
        assert!(total <= v.len());
        assert!(linear_fraction(&v, 6, 1e-9) <= 1.0);
    }

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
        assert!(p.rate_p999.is_some());
        assert!(p.noise_mad.is_some());
    }

    #[test]
    fn noise_sigma_recovers_injected_noise() {
        use crate::synth::{generate, SynthSpec};
        let f = generate(&SynthSpec { n: 20_000, noise_sd: 0.5, ..SynthSpec::default() });
        let p = Profile::compute(&f);
        let sigma = p.noise_mad.unwrap();
        assert!((sigma - 0.5).abs() < 0.05, "noise sigma {sigma}");
    }

    #[test]
    fn linear_runs_detect_ramps_not_flats() {
        let mut v: Vec<f64> = (0..40).map(|i| ((i * 7) % 11) as f64).collect();
        for (k, x) in v.iter_mut().enumerate().take(30).skip(10) {
            *x = k as f64 * 0.5; // ramp of 20 samples
        }
        for x in v.iter_mut().take(38).skip(32) {
            *x = 4.0; // flat run of 6, must not count
        }
        let runs = linear_runs(&v, 6, 1e-9);
        assert_eq!(runs.len(), 1, "{runs:?}");
        assert!(runs[0].0 >= 9 && runs[0].1 <= 31, "{runs:?}");
        assert!(runs[0].1 - runs[0].0 >= 19);
    }
}
