//! Chart downsampling (ADR-0008). M4 keeps, per time bucket, the first, minimum, maximum and
//! last samples so spikes, flatlines and gaps stay visible at any zoom level.

use crate::frame::SeriesFrame;
use crate::quality::Quality;

/// M4 downsampling of a *sorted* frame into at most `buckets` equal-width time buckets.
/// Returns `(ts, values)` with up to four points per bucket, in time order, without
/// duplicates. NaN values are skipped for min/max but kept as first/last so gaps stay visible.
pub fn m4(frame: &SeriesFrame, buckets: usize) -> (Vec<i64>, Vec<f64>) {
    let n = frame.len();
    if n == 0 || buckets == 0 {
        return (Vec::new(), Vec::new());
    }
    if n <= buckets * 4 {
        return (frame.ts.clone(), frame.values.clone());
    }
    let (t0, t1) = (frame.ts[0], frame.ts[n - 1]);
    let span = t1.saturating_sub(t0).max(1) as f64;
    let mut out_idx: Vec<usize> = Vec::with_capacity(buckets * 4);
    let mut i = 0usize;
    for b in 0..buckets {
        let start = i;
        if b + 1 == buckets {
            // The last bucket takes every remaining sample, including one at `i64::MAX`.
            i = n;
        } else {
            let end_ts = t0.saturating_add(((span * (b + 1) as f64) / buckets as f64) as i64);
            while i < n && frame.ts[i] < end_ts {
                i += 1;
            }
        }
        if start == i {
            continue;
        }
        let (mut imin, mut imax) = (None::<usize>, None::<usize>);
        for j in start..i {
            let v = frame.values[j];
            if !v.is_finite() {
                continue;
            }
            if imin.is_none_or(|k| v < frame.values[k]) {
                imin = Some(j);
            }
            if imax.is_none_or(|k| v > frame.values[k]) {
                imax = Some(j);
            }
        }
        let mut picks = vec![start, i - 1];
        picks.extend(imin);
        picks.extend(imax);
        picks.sort_unstable();
        picks.dedup();
        out_idx.extend(picks);
    }
    let ts = out_idx.iter().map(|&k| frame.ts[k]).collect();
    let values = out_idx.iter().map(|&k| frame.values[k]).collect();
    (ts, values)
}

/// Bin of `t` among `buckets` equal bins of `[from, to)`; the caller keeps `t` in the window.
fn bin_of(t: i64, from: i64, to: i64, buckets: usize) -> usize {
    let span = (to as i128 - from as i128).max(1);
    let b = (t as i128 - from as i128) * buckets as i128 / span;
    (b.max(0) as usize).min(buckets - 1)
}

/// Start of bin `b` of `buckets` equal bins of `[from, to)`.
fn bin_start(b: usize, from: i64, to: i64, buckets: usize) -> i64 {
    let span = to as i128 - from as i128;
    (from as i128 + span * b as i128 / buckets as i128) as i64
}

/// Index range of the samples of a sorted frame inside `[from, to)`.
fn window_range(frame: &SeriesFrame, from: i64, to: i64) -> std::ops::Range<usize> {
    let lo = frame.ts.partition_point(|&t| t < from);
    let hi = frame.ts.partition_point(|&t| t < to);
    lo..hi.max(lo)
}

/// M4 of a *sorted* frame on `buckets` equal bins of the window `[from, to)` (spec 025).
///
/// The bins come from the window, not the data, so frames asked for the same window share bin
/// edges (spec 018). Per bin, the first, minimum, maximum and last samples are kept, in time
/// order and without duplicates; NaN values are never the minimum or maximum but stay as first
/// or last, so missing values still break the line. A window with at most `4 × buckets` samples
/// is returned whole. Rows outside the window are ignored.
pub fn m4_window(frame: &SeriesFrame, from: i64, to: i64, buckets: usize) -> (Vec<i64>, Vec<f64>) {
    let range = window_range(frame, from, to);
    if range.is_empty() || buckets == 0 || to <= from {
        return (Vec::new(), Vec::new());
    }
    if range.len() <= buckets * 4 {
        return (frame.ts[range.clone()].to_vec(), frame.values[range].to_vec());
    }
    let mut out_idx: Vec<usize> = Vec::with_capacity(buckets * 4);
    let mut start = range.start;
    while start < range.end {
        let b = bin_of(frame.ts[start], from, to, buckets);
        let mut end = start + 1;
        let (mut imin, mut imax) = (None::<usize>, None::<usize>);
        let mut j = start;
        loop {
            let v = frame.values[j];
            if v.is_finite() {
                if imin.is_none_or(|k| v < frame.values[k]) {
                    imin = Some(j);
                }
                if imax.is_none_or(|k| v > frame.values[k]) {
                    imax = Some(j);
                }
            }
            if end >= range.end || bin_of(frame.ts[end], from, to, buckets) != b {
                break;
            }
            j = end;
            end += 1;
        }
        let mut picks = [Some(start), imin, imax, Some(end - 1)];
        picks.sort_unstable();
        let mut last = None;
        for k in picks.into_iter().flatten() {
            if last != Some(k) {
                out_idx.push(k);
                last = Some(k);
            }
        }
        start = end;
    }
    let ts = out_idx.iter().map(|&k| frame.ts[k]).collect();
    let values = out_idx.iter().map(|&k| frame.values[k]).collect();
    (ts, values)
}

/// Insert a NaN point one nanosecond after every sample followed by a step longer than
/// `max_step`, so a chart breaks its line across the gap instead of bridging it.
pub fn gap_breaks(ts: &[i64], values: &[f64], max_step: i64) -> (Vec<i64>, Vec<f64>) {
    let mut out_ts = Vec::with_capacity(ts.len() + 16);
    let mut out_values = Vec::with_capacity(ts.len() + 16);
    for i in 0..ts.len() {
        out_ts.push(ts[i]);
        out_values.push(values[i]);
        if i + 1 < ts.len() && ts[i + 1].saturating_sub(ts[i]) > max_step && !values[i].is_nan() {
            out_ts.push(ts[i] + 1);
            out_values.push(f64::NAN);
        }
    }
    (out_ts, out_values)
}

/// A run of samples of one non-good quality class, `[start, end)` in ns.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct QualityRun {
    pub start: i64,
    pub end: i64,
    pub quality: Quality,
}

/// Rank of a quality class for the rug: the worst class present in a bin wins.
fn rug_rank(q: Quality) -> u8 {
    match q {
        Quality::Good => 0,
        Quality::Uncertain => 1,
        Quality::Estimated => 2,
        Quality::Bad => 3,
    }
}

/// Runs of non-good quality of a *sorted* frame at the resolution of `buckets` equal bins of
/// `[from, to)` (spec 025). Each bin holding a non-good sample takes its worst class (bad >
/// estimated > uncertain). Consecutive such bins of one class merge into one run spanning their
/// bins unless a good sample lies between them, so sparse samples still make one run.
pub fn quality_runs(frame: &SeriesFrame, from: i64, to: i64, buckets: usize) -> Vec<QualityRun> {
    let range = window_range(frame, from, to);
    if range.is_empty() || buckets == 0 || to <= from {
        return Vec::new();
    }
    // (bin, worst class, whether a good sample lies between it and the previous entry).
    let mut bins: Vec<(usize, Quality, bool)> = Vec::new();
    let mut good_since = false;
    for i in range {
        let q = frame.quality[i];
        if q == Quality::Good {
            good_since = true;
            continue;
        }
        let b = bin_of(frame.ts[i], from, to, buckets);
        match bins.last_mut() {
            Some((lb, lq, _)) if *lb == b => {
                if rug_rank(q) > rug_rank(*lq) {
                    *lq = q;
                }
            }
            _ => bins.push((b, q, good_since)),
        }
        good_since = false;
    }
    let mut runs: Vec<QualityRun> = Vec::new();
    for (b, q, broken) in bins {
        let end = if b + 1 == buckets { to } else { bin_start(b + 1, from, to, buckets) };
        match runs.last_mut() {
            Some(run) if run.quality == q && !broken => run.end = end,
            _ => runs.push(QualityRun { start: bin_start(b, from, to, buckets), end, quality: q }),
        }
    }
    runs
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::synth::{generate, inject, Rng, SynthSpec};

    #[test]
    fn keeps_extremes_and_bounds_size() {
        let mut f = generate(&SynthSpec { n: 10_000, ..SynthSpec::default() });
        let mut rng = Rng::new(9);
        let idx = inject::spikes(&mut f, &mut rng, 3, 100.0);
        let (ts, values) = m4(&f, 200);
        assert!(ts.len() <= 800);
        assert!(ts.windows(2).all(|w| w[0] < w[1]));
        for i in idx {
            assert!(values.iter().any(|v| (*v - f.values[i]).abs() < 1e-12), "spike {i} lost");
        }
    }

    #[test]
    fn small_input_passthrough() {
        let f = generate(&SynthSpec { n: 100, ..SynthSpec::default() });
        let (ts, _) = m4(&f, 100);
        assert_eq!(ts.len(), 100);
    }

    fn frame(ts: Vec<i64>, values: Vec<f64>, quality: Vec<Quality>) -> SeriesFrame {
        SeriesFrame::new(crate::frame::SeriesMeta::new("s"), ts, values, quality).unwrap()
    }

    #[test]
    fn m4_window_bins_from_window() {
        // 1000 samples at 0..1000 in a window [0, 2000) of 10 bins: only bins 0-4 hold data,
        // each with 100 samples, so each yields at most 4 points from its own bin.
        let ts: Vec<i64> = (0..1000).collect();
        let values: Vec<f64> = ts.iter().map(|t| (*t as f64 * 0.37).sin()).collect();
        let f = frame(ts, values, vec![Quality::Good; 1000]);
        let (out, _) = m4_window(&f, 0, 2000, 10);
        assert!(out.len() <= 5 * 4);
        assert!(out.iter().all(|t| *t < 1000));
        assert_eq!(out.first(), Some(&0));
        assert_eq!(out.last(), Some(&999));
        assert!(out.windows(2).all(|w| w[0] < w[1]));
        // Bin 0 is [0, 200): both 0 and 199 are kept as its first and last.
        assert!(out.contains(&199) && out.contains(&200));
    }

    #[test]
    fn m4_window_keeps_spikes() {
        let mut f = generate(&SynthSpec { n: 100_000, ..SynthSpec::default() });
        let mut rng = Rng::new(3);
        let idx = inject::spikes(&mut f, &mut rng, 5, 100.0);
        let (from, to) = (f.ts[0], f.ts[f.len() - 1] + 1);
        let (ts, values) = m4_window(&f, from, to, 300);
        assert!(ts.len() <= 1200);
        for i in idx {
            assert!(values.iter().any(|v| (*v - f.values[i]).abs() < 1e-12), "spike {i} lost");
        }
    }

    #[test]
    fn m4_window_shared_edges() {
        // Two series with different sampling share bin edges: every output point of each
        // lies in the bin its timestamp maps to, and both cover the same bins.
        let a = frame((0..10_000).map(|i| i * 7).collect(), vec![1.0; 10_000], vec![Quality::Good; 10_000]);
        let b = frame((0..7_000).map(|i| i * 10 + 3).collect(), vec![2.0; 7_000], vec![Quality::Good; 7_000]);
        let (from, to, n) = (0, 70_000, 50);
        let bins = |ts: &[i64]| {
            let mut v: Vec<usize> = ts.iter().map(|t| bin_of(*t, from, to, n)).collect();
            v.dedup();
            v
        };
        let (ta, _) = m4_window(&a, from, to, n);
        let (tb, _) = m4_window(&b, from, to, n);
        assert_eq!(bins(&ta), (0..n).collect::<Vec<_>>());
        assert_eq!(bins(&ta), bins(&tb));
        assert_eq!(bin_start(10, from, to, n), 14_000);
    }

    #[test]
    fn m4_window_ignores_outside_rows() {
        let f = frame((0..100).collect(), (0..100).map(|v| v as f64).collect(), vec![Quality::Good; 100]);
        let (ts, values) = m4_window(&f, 10, 20, 100);
        assert_eq!(ts, (10..20).collect::<Vec<_>>());
        assert_eq!(values[0], 10.0);
        assert!(m4_window(&f, 200, 300, 10).0.is_empty());
        assert!(m4_window(&f, 20, 10, 10).0.is_empty());
    }

    #[test]
    fn m4_window_nan_is_never_extreme() {
        let mut values: Vec<f64> = (0..1000).map(|v| v as f64).collect();
        values[500] = f64::NAN;
        let f = frame((0..1000).collect(), values, vec![Quality::Good; 1000]);
        let (ts, out) = m4_window(&f, 0, 1000, 2);
        // Bin 1 is [500, 1000): its first sample is the NaN, kept; min is 501.
        assert!(ts.contains(&500) && ts.contains(&501));
        assert_eq!(out.iter().filter(|v| v.is_nan()).count(), 1);
    }

    #[test]
    fn gap_breaks_inserts_nan() {
        let (ts, values) = gap_breaks(&[0, 10, 20, 100, 110], &[1.0, 2.0, 3.0, 4.0, 5.0], 30);
        assert_eq!(ts, vec![0, 10, 20, 21, 100, 110]);
        assert!(values[3].is_nan());
        // A NaN already ends the line: no second break.
        let (ts, _) = gap_breaks(&[0, 100], &[f64::NAN, 1.0], 30);
        assert_eq!(ts, vec![0, 100]);
    }

    #[test]
    fn quality_runs_merge_and_worst_class() {
        use Quality::*;
        // 10 bins of 10 ns over [0, 100).
        let ts: Vec<i64> = (0..100).collect();
        let mut q = vec![Good; 100];
        for item in q.iter_mut().take(30).skip(10) {
            *item = Uncertain; // bins 1-2
        }
        q[25] = Bad; // bin 2 becomes bad
        q[31] = Uncertain; // bin 3 uncertain
        q[90] = Estimated; // bin 9
        let f = frame(ts, vec![0.0; 100], q);
        let runs = quality_runs(&f, 0, 100, 10);
        let got: Vec<(i64, i64, Quality)> = runs.iter().map(|r| (r.start, r.end, r.quality)).collect();
        assert_eq!(got, vec![(10, 20, Uncertain), (20, 30, Bad), (30, 40, Uncertain), (90, 100, Estimated)]);
        // Good samples between bad bins keep them apart, even in adjacent bins.
        let mut q2 = vec![Good; 100];
        q2[5] = Bad;
        q2[15] = Bad;
        q2[35] = Bad;
        let runs = quality_runs(&frame((0..100).collect(), vec![0.0; 100], q2), 0, 100, 10);
        let got: Vec<(i64, i64)> = runs.iter().map(|r| (r.start, r.end)).collect();
        assert_eq!(got, vec![(0, 10), (10, 20), (30, 40)]);
    }

    #[test]
    fn quality_runs_span_empty_bins_between_sparse_samples() {
        use Quality::*;
        // Samples every 30 ns in 100 bins of 10 ns: bad samples at 60..=150 sit in bins with
        // empty bins between them, yet make one run.
        let ts: Vec<i64> = (0..10).map(|i| i * 30).collect();
        let q: Vec<Quality> = (0..10).map(|i| if (2..=5).contains(&i) { Bad } else { Good }).collect();
        let runs = quality_runs(&frame(ts, vec![0.0; 10], q), 0, 1000, 100);
        let got: Vec<(i64, i64, Quality)> = runs.iter().map(|r| (r.start, r.end, r.quality)).collect();
        assert_eq!(got, vec![(60, 160, Bad)]);
    }
}
