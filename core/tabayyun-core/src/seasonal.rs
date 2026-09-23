//! Seasonality detection for the baseline profile and `tby.seasonality_break` (spec 012).
//!
//! Pure functions over a series regularised to a fixed step (mean per epoch-anchored bin,
//! NaN for empty bins). Every mean and variance skips NaN bins.
//!
//! A candidate period counts when the autocorrelation at its lag is at least [`MIN_ACF`]
//! *after* removing the trend with a centred moving average of one period: the raw ACF of a
//! random walk or of slowly drifting wind is high at every lag and would read as a rhythm.
//! Harmonics make "the highest ACF" ambiguous (a daily rhythm repeats at 7 d as well), so
//! [`detect`] takes the shortest significant candidate. Strength is Hyndman's
//! F_S = max(0, 1 − Var(R) / Var(S + R)) from a classical decomposition. Spec 012, edits.

use crate::frame::SeriesFrame;
use crate::time::{NS_PER_DAY, NS_PER_HOUR};

/// Candidate periods of the profile: a day, a week, a year.
pub const DEFAULT_CANDIDATES_NS: [i64; 3] = [NS_PER_DAY, 7 * NS_PER_DAY, 365 * NS_PER_DAY];
/// A detrended autocorrelation at the period's lag below this is no rhythm.
pub const MIN_ACF: f64 = 0.3;
/// Largest regularised grid (about 57 years of hours): a stray timestamp far from the rest
/// would otherwise allocate a grid of mostly empty bins, on every profile.
pub const MAX_BINS: usize = 500_000;
/// A period needs at least this many steps, and the data at least this many periods.
const MIN_STEPS_PER_PERIOD: usize = 4;
const MIN_PERIODS: usize = 3;

/// The step a frame is regularised to before the ACF: its expected interval, or one hour
/// when finer (bounds the cost of long high-rate series).
pub fn step_for(frame: &SeriesFrame) -> Option<i64> {
    frame.expected_interval_ns().filter(|&i| i > 0).map(|i| i.max(NS_PER_HOUR))
}

/// Mean of the usable finite values per bin of `step_ns`, bins anchored at multiples of the
/// step since the epoch. Returns the first bin's start and one value per bin (NaN when empty);
/// no values when the span would need more than [`MAX_BINS`] bins.
pub fn regularise(frame: &SeriesFrame, step_ns: i64) -> (i64, Vec<f64>) {
    let (Some(first), Some(last)) = (frame.first_ts(), frame.last_ts()) else { return (0, Vec::new()) };
    if step_ns <= 0 {
        return (0, Vec::new());
    }
    let Some(start) = first.div_euclid(step_ns).checked_mul(step_ns) else { return (0, Vec::new()) };
    let Some(n) = last
        .checked_sub(start)
        .and_then(|span| (span / step_ns).checked_add(1))
        .and_then(|n| usize::try_from(n).ok())
        .filter(|&n| n <= MAX_BINS)
    else {
        return (start, Vec::new());
    };
    let (mut sum, mut count) = (vec![0.0; n], vec![0u32; n]);
    for i in 0..frame.len() {
        let v = frame.values[i];
        if !v.is_finite() || !frame.quality[i].is_usable() {
            continue;
        }
        let k = ((frame.ts[i] - start) / step_ns) as usize;
        if k < n {
            sum[k] += v;
            count[k] += 1;
        }
    }
    (start, sum.iter().zip(&count).map(|(s, &c)| if c > 0 { s / c as f64 } else { f64::NAN }).collect())
}

/// Pearson correlation of `values[i]` and `values[i + lag]` over the pairs where both are
/// finite. `None` with fewer than three pairs or a constant side.
pub fn acf_at(values: &[f64], lag: usize) -> Option<f64> {
    if lag == 0 || lag >= values.len() {
        return None;
    }
    let pairs: Vec<(f64, f64)> = (0..values.len() - lag)
        .map(|i| (values[i], values[i + lag]))
        .filter(|(a, b)| a.is_finite() && b.is_finite())
        .collect();
    if pairs.len() < 3 {
        return None;
    }
    let n = pairs.len() as f64;
    let (ma, mb) = (pairs.iter().map(|p| p.0).sum::<f64>() / n, pairs.iter().map(|p| p.1).sum::<f64>() / n);
    let (mut sab, mut saa, mut sbb) = (0.0, 0.0, 0.0);
    for (a, b) in &pairs {
        sab += (a - ma) * (b - mb);
        saa += (a - ma).powi(2);
        sbb += (b - mb).powi(2);
    }
    let d = (saa * sbb).sqrt();
    (d > 0.0).then(|| sab / d)
}

/// Centred moving average of length `period` (a 2×`period` average for an even period, so
/// it stays centred), over the finite values of each window; NaN where fewer than half of
/// the window's weight is finite or the window leaves the series.
pub fn moving_average(values: &[f64], period: usize) -> Vec<f64> {
    let n = values.len();
    let mut out = vec![f64::NAN; n];
    if period == 0 || n < period {
        return out;
    }
    // Prefix sums of finite values and of their count.
    let mut ps = vec![0.0; n + 1];
    let mut pc = vec![0.0; n + 1];
    for i in 0..n {
        let ok = values[i].is_finite();
        ps[i + 1] = ps[i] + if ok { values[i] } else { 0.0 };
        pc[i + 1] = pc[i] + if ok { 1.0 } else { 0.0 };
    }
    let range = |a: usize, b: usize| (ps[b] - ps[a], pc[b] - pc[a]); // [a, b)
    let h = period / 2;
    for (i, slot) in out.iter_mut().enumerate().take(n.saturating_sub(h)).skip(h) {
        let (s, w) = if period % 2 == 1 {
            range(i - h, i + h + 1)
        } else {
            // Weights 0.5 at both ends, 1 in between: period + 1 points, total weight period.
            let (mut s, mut w) = range(i - h + 1, i + h);
            for j in [i - h, i + h] {
                if values[j].is_finite() {
                    s += 0.5 * values[j];
                    w += 0.5;
                }
            }
            (s, w)
        };
        if w >= period as f64 / 2.0 {
            *slot = s / w;
        }
    }
    out
}

/// The values minus their centred moving average of one period.
pub fn detrend(values: &[f64], period: usize) -> Vec<f64> {
    values.iter().zip(moving_average(values, period)).map(|(v, t)| v - t).collect()
}

/// Autocorrelation at lag `period` of the series with its trend removed.
pub fn detrended_acf(values: &[f64], period: usize) -> Option<f64> {
    acf_at(&detrend(values, period), period)
}

/// Hyndman's seasonal strength F_S for `period` steps: classical decomposition with the
/// seasonal component as the mean detrended value per phase. `None` when fewer than two
/// periods of detrended values or a phase without values; 0 for a series without variance
/// (a flat stretch has no rhythm).
pub fn seasonal_strength(values: &[f64], period: usize) -> Option<f64> {
    if period < 2 {
        return None;
    }
    let d = detrend(values, period);
    let finite = d.iter().filter(|x| x.is_finite()).count();
    if finite < 2 * period {
        return None;
    }
    let mut phase_sum = vec![0.0; period];
    let mut phase_n = vec![0usize; period];
    for (i, x) in d.iter().enumerate() {
        if x.is_finite() {
            phase_sum[i % period] += x;
            phase_n[i % period] += 1;
        }
    }
    if phase_n.contains(&0) {
        return None;
    }
    let mut s: Vec<f64> = phase_sum.iter().zip(&phase_n).map(|(a, &c)| a / c as f64).collect();
    let centre = s.iter().sum::<f64>() / period as f64;
    s.iter_mut().for_each(|x| *x -= centre);
    let (sr, r): (Vec<f64>, Vec<f64>) =
        d.iter().enumerate().filter(|(_, x)| x.is_finite()).map(|(i, &x)| (x, x - s[i % period])).unzip();
    let var = |xs: &[f64]| {
        let m = xs.iter().sum::<f64>() / xs.len() as f64;
        xs.iter().map(|x| (x - m).powi(2)).sum::<f64>() / xs.len() as f64
    };
    let total = var(&sr);
    if total <= f64::EPSILON * (1.0 + sr.iter().map(|x| x.abs()).fold(0.0, f64::max)).powi(2) {
        return Some(0.0);
    }
    Some((1.0 - var(&r) / total).clamp(0.0, 1.0))
}

/// Period (ns) and strength of the shortest candidate with a rhythm, on values regularised to
/// `step_ns`. A candidate is tried only when it is at least 4 steps long and the values span
/// at least 3 of its periods.
pub fn detect_values(values: &[f64], step_ns: i64, candidates_ns: &[i64]) -> Option<(i64, f64)> {
    if step_ns <= 0 {
        return None;
    }
    let mut candidates = candidates_ns.to_vec();
    candidates.sort_unstable();
    candidates.into_iter().filter(|&c| c > 0).find_map(|c| {
        let period = (c as f64 / step_ns as f64).round() as usize;
        if period < MIN_STEPS_PER_PERIOD || values.len() < MIN_PERIODS * period {
            return None;
        }
        let acf = detrended_acf(values, period)?;
        if acf < MIN_ACF {
            return None;
        }
        seasonal_strength(values, period).map(|s| (c, s))
    })
}

/// Dominant period and strength of a frame (see [`detect_values`]), regularised to
/// [`step_for`] the frame.
pub fn detect(frame: &SeriesFrame, candidates_ns: &[i64]) -> Option<(i64, f64)> {
    let step = step_for(frame)?;
    let (_, values) = regularise(frame, step);
    detect_values(&values, step, candidates_ns)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::frame::SeriesMeta;
    use crate::synth::Rng;

    const H: usize = 24;

    fn hourly(values: Vec<f64>) -> SeriesFrame {
        let ts = (0..values.len() as i64).map(|i| 1_704_326_400 * 1_000_000_000 + i * NS_PER_HOUR).collect();
        SeriesFrame::with_default_quality(SeriesMeta::new("s"), ts, values).unwrap()
    }

    fn daily_sine(n: usize, noise: f64, seed: u64) -> Vec<f64> {
        let mut rng = Rng::new(seed);
        (0..n)
            .map(|i| 10.0 * (i as f64 / H as f64 * std::f64::consts::TAU).sin() + noise * rng.normal())
            .collect()
    }

    #[test]
    fn detects_daily() {
        let f = hourly(daily_sine(8 * 7 * H, 2.0, 1));
        let (period, strength) = detect(&f, &DEFAULT_CANDIDATES_NS).unwrap();
        assert_eq!(period, NS_PER_DAY);
        assert!(strength > 0.8, "{strength}");
    }

    #[test]
    fn ignores_noise() {
        let mut rng = Rng::new(2);
        let f = hourly((0..8 * 7 * H).map(|_| rng.normal()).collect());
        assert_eq!(detect(&f, &DEFAULT_CANDIDATES_NS), None);
    }

    #[test]
    fn ignores_random_walk() {
        // The raw ACF of a random walk is about 0.9 at a lag of one day.
        let mut rng = Rng::new(3);
        let mut x = 0.0;
        let walk: Vec<f64> = (0..8 * 7 * H)
            .map(|_| {
                x += rng.normal();
                x
            })
            .collect();
        assert!(acf_at(&walk, H).unwrap() > 0.5);
        assert_eq!(detect(&hourly(walk), &DEFAULT_CANDIDATES_NS), None);
    }

    #[test]
    fn weekly_only_rhythm_is_weekly() {
        // Weekdays at 50, weekends at 10: no daily shape.
        let mut rng = Rng::new(4);
        let v = (0..12 * 7 * H)
            .map(|i| if (i / H) % 7 >= 5 { 10.0 } else { 50.0 } + 2.0 * rng.normal())
            .collect();
        let (period, strength) = detect(&hourly(v), &DEFAULT_CANDIDATES_NS).unwrap();
        assert_eq!(period, 7 * NS_PER_DAY);
        assert!(strength > 0.9, "{strength}");
    }

    #[test]
    fn strength_bounds() {
        let sine = daily_sine(4 * 7 * H, 0.0, 5);
        let s = seasonal_strength(&sine, H).unwrap();
        assert!((0.99..=1.0).contains(&s), "{s}");
        assert_eq!(seasonal_strength(&vec![3.0; 4 * 7 * H], H), Some(0.0));
        let mut rng = Rng::new(6);
        let noise: Vec<f64> = (0..4 * 7 * H).map(|_| rng.normal()).collect();
        let s = seasonal_strength(&noise, H).unwrap();
        assert!((0.0..0.2).contains(&s), "{s}");
        // Too short for two periods of detrended values.
        assert_eq!(seasonal_strength(&sine[..2 * H], H), None);
    }

    #[test]
    fn nan_bins_skipped() {
        let mut v = daily_sine(8 * 7 * H, 1.0, 7);
        for i in (0..v.len()).step_by(5) {
            v[i] = f64::NAN;
        }
        let (period, strength) = detect_values(&v, NS_PER_HOUR, &DEFAULT_CANDIDATES_NS).unwrap();
        assert_eq!(period, NS_PER_DAY);
        assert!(strength > 0.8, "{strength}");
    }

    #[test]
    fn regularise_to_hourly_means() {
        // Ten-minute samples 0..6 per hour average to 2.5; a missing hour stays NaN.
        let t0 = 1_704_326_400 * 1_000_000_000;
        let mut ts = Vec::new();
        let mut values = Vec::new();
        for h in [0i64, 2] {
            for k in 0..6 {
                ts.push(t0 + h * NS_PER_HOUR + k * 10 * 60 * 1_000_000_000);
                values.push(k as f64);
            }
        }
        let f = SeriesFrame::with_default_quality(SeriesMeta::new("s"), ts, values).unwrap();
        let (start, v) = regularise(&f, NS_PER_HOUR);
        assert_eq!(start, t0);
        assert_eq!(v.len(), 3);
        assert_eq!(v[0], 2.5);
        assert!(v[1].is_nan());
        assert_eq!(v[2], 2.5);
        assert_eq!(step_for(&f), Some(NS_PER_HOUR));
    }

    #[test]
    fn huge_span_is_not_regularised() {
        // A stray timestamp at the end of time must not allocate a grid of empty hours.
        let f = SeriesFrame::with_default_quality(
            SeriesMeta::new("s"),
            vec![0, NS_PER_HOUR, i64::MAX],
            vec![1.0, 2.0, 3.0],
        )
        .unwrap();
        assert!(regularise(&f, NS_PER_HOUR).1.is_empty());
        assert_eq!(detect(&f, &DEFAULT_CANDIDATES_NS), None);
        // Nor may timestamps far apart on both sides overflow the span (2^63 does not fit).
        let far = 1i64 << 62;
        let f = SeriesFrame::with_default_quality(SeriesMeta::new("s"), vec![-far, 0, far], vec![1.0; 3])
            .unwrap();
        assert!(regularise(&f, NS_PER_HOUR).1.is_empty());
        assert_eq!(detect(&f, &DEFAULT_CANDIDATES_NS), None);
        // A one-nanosecond step over the whole positive range: the bin count itself overflows.
        let f = SeriesFrame::with_default_quality(SeriesMeta::new("s"), vec![0, 1, i64::MAX], vec![1.0; 3])
            .unwrap();
        assert!(regularise(&f, 1).1.is_empty());
    }

    #[test]
    fn moving_average_even_period_is_centred() {
        // A straight line is its own centred average.
        let line: Vec<f64> = (0..50).map(|i| i as f64).collect();
        let ma = moving_average(&line, 24);
        assert!(ma[..12].iter().all(|x| x.is_nan()));
        assert!((12..38).all(|i| (ma[i] - i as f64).abs() < 1e-9));
    }
}
