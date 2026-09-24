//! Chart downsampling (ADR-0008). M4 keeps, per time bucket, the first, minimum, maximum and
//! last samples so spikes, flatlines and gaps stay visible at any zoom level.

use crate::frame::SeriesFrame;

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
}
