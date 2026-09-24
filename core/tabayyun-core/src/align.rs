//! Put several series on one time grid for cross-series checks (spec 008).
//!
//! Bins are `[k·grid, (k+1)·grid)` anchored at the Unix epoch, so members sampled at offset
//! timestamps (a meter at :00, its twin at :07) land in the same bins. The grid is the
//! coarsest expected interval among the members unless the caller fixes it: averaging a fast
//! series down to a slow one's rate is honest, the reverse would invent samples.
//!
//! `align` never invents data: a bin without a usable, finite sample is NaN, and cross checks
//! ignore bins where a member they need is NaN. A span needing more than [`MAX_BINS`] bins
//! aligns to nothing, so the cross checks stay silent on it.

use crate::frame::SeriesFrame;

/// Most bins `align` builds: 10 million (19 years of minutes). A stray far timestamp would
/// otherwise ask for a grid across centuries; such a group aligns to nothing instead.
pub const MAX_BINS: usize = 10_000_000;

/// Members on one grid: `columns[j][i]` is member `j`'s mean in the bin starting at `ts[i]`.
#[derive(Debug, Clone, PartialEq)]
pub struct Aligned {
    pub ts: Vec<i64>,
    pub grid_ns: i64,
    pub columns: Vec<Vec<f64>>,
}

impl Aligned {
    pub fn len(&self) -> usize {
        self.ts.len()
    }

    pub fn is_empty(&self) -> bool {
        self.ts.is_empty()
    }

    /// Whether every member has a value in bin `i`.
    pub fn complete(&self, i: usize) -> bool {
        self.columns.iter().all(|c| c[i].is_finite())
    }
}

/// The grid `align` would use: `grid_ns` when positive, else the coarsest expected interval
/// among the members (metadata first, then the data's modal interval).
pub fn grid_for(frames: &[&SeriesFrame], grid_ns: Option<i64>) -> Option<i64> {
    grid_ns.filter(|g| *g > 0).or_else(|| frames.iter().filter_map(|f| f.expected_interval_ns()).max())
}

/// Align `frames` on a common grid (see the module docs). With no usable grid (no member has
/// an expected interval and none was given) the result is empty with `grid_ns = 0`.
pub fn align(frames: &[&SeriesFrame], grid_ns: Option<i64>) -> Aligned {
    let empty = |grid_ns| Aligned { ts: Vec::new(), grid_ns, columns: vec![Vec::new(); frames.len()] };
    let Some(grid) = grid_for(frames, grid_ns) else {
        return empty(0);
    };
    let bin = |t: i64| t.div_euclid(grid);
    let bounds = frames.iter().flat_map(|f| f.ts.iter().map(|&t| bin(t)));
    let (Some(lo), Some(hi)) = (bounds.clone().min(), bounds.max()) else {
        return empty(grid);
    };
    let Some(n) =
        hi.checked_sub(lo).and_then(|d| usize::try_from(d).ok()).map(|d| d + 1).filter(|&n| n <= MAX_BINS)
    else {
        return empty(grid);
    };
    let columns = frames
        .iter()
        .map(|f| {
            let mut sum = vec![0.0; n];
            let mut count = vec![0u32; n];
            for i in 0..f.len() {
                let v = f.values[i];
                if v.is_finite() && f.quality[i].is_usable() {
                    let k = (bin(f.ts[i]) - lo) as usize;
                    sum[k] += v;
                    count[k] += 1;
                }
            }
            sum.iter().zip(&count).map(|(s, &c)| if c > 0 { s / c as f64 } else { f64::NAN }).collect()
        })
        .collect();
    // The first bin can start before `i64::MIN`; its start clamps there.
    Aligned { ts: (lo..=hi).map(|k| k.saturating_mul(grid)).collect(), grid_ns: grid, columns }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::frame::SeriesMeta;
    use crate::quality::Quality;

    const MIN: i64 = 60_000_000_000;

    fn frame(id: &str, ts: &[i64], values: &[f64]) -> SeriesFrame {
        SeriesFrame::with_default_quality(SeriesMeta::new(id), ts.to_vec(), values.to_vec()).unwrap()
    }

    fn same(a: &[f64], b: &[f64]) -> bool {
        a.len() == b.len()
            && a.iter().zip(b).all(|(x, y)| (x.is_nan() && y.is_nan()) || (x - y).abs() < 1e-12)
    }

    #[test]
    fn grid_is_the_coarsest_interval_and_bins_hold_means() {
        // `fast` every minute, `slow` every 5 minutes: the grid is 5 minutes.
        let fast_ts: Vec<i64> = (0..15).map(|i| i * MIN).collect();
        let fast = frame("fast", &fast_ts, &(0..15).map(f64::from).collect::<Vec<_>>());
        let slow = frame("slow", &[0, 5 * MIN, 10 * MIN], &[100.0, 200.0, 300.0]);
        let a = align(&[&fast, &slow], None);
        assert_eq!(a.grid_ns, 5 * MIN);
        assert_eq!(a.ts, vec![0, 5 * MIN, 10 * MIN]);
        assert!(same(&a.columns[0], &[2.0, 7.0, 12.0]));
        assert!(same(&a.columns[1], &[100.0, 200.0, 300.0]));
        assert!((0..3).all(|i| a.complete(i)));
    }

    #[test]
    fn empty_bins_are_nan_and_unusable_samples_are_ignored() {
        let mut a_frame = frame("a", &[0, MIN, 2 * MIN, 4 * MIN], &[1.0, f64::NAN, 3.0, 5.0]);
        a_frame.quality[2] = Quality::Bad;
        let b_frame = frame("b", &[0, MIN, 2 * MIN, 3 * MIN, 4 * MIN], &[1.0; 5]);
        let a = align(&[&a_frame, &b_frame], Some(MIN));
        assert_eq!(a.len(), 5);
        // Bin 1 is NaN in the data, bin 2 is bad quality, bin 3 has no sample.
        assert!(same(&a.columns[0], &[1.0, f64::NAN, f64::NAN, f64::NAN, 5.0]));
        assert_eq!((0..5).filter(|&i| a.complete(i)).collect::<Vec<_>>(), vec![0, 4]);
    }

    #[test]
    fn offset_timestamps_line_up_on_epoch_anchored_bins() {
        let hour = 60 * MIN;
        let base = 1_700_000_000_000_000_000 / hour * hour;
        let on_the_hour: Vec<i64> = (0..3).map(|i| base + i * hour).collect();
        let seven_past: Vec<i64> = on_the_hour.iter().map(|t| t + 7 * MIN).collect();
        let x = frame("x", &on_the_hour, &[1.0, 2.0, 3.0]);
        let y = frame("y", &seven_past, &[10.0, 20.0, 30.0]);
        let a = align(&[&x, &y], None);
        assert_eq!(a.grid_ns, hour);
        assert_eq!(a.ts, on_the_hour);
        assert!((0..3).all(|i| a.complete(i)));
        assert!(same(&a.columns[1], &[10.0, 20.0, 30.0]));
    }

    #[test]
    fn negative_timestamps_and_no_grid() {
        let x = frame("x", &[-MIN, 0], &[1.0, 2.0]);
        let a = align(&[&x], Some(MIN));
        assert_eq!(a.ts, vec![-MIN, 0]);
        // A single sample has no modal interval, so there is no grid to align on.
        let lone = frame("lone", &[0], &[1.0]);
        let a = align(&[&lone], None);
        assert_eq!((a.grid_ns, a.len(), a.columns.len()), (0, 0, 1));
        let empty = frame("e", &[], &[]);
        assert!(align(&[&empty], Some(MIN)).is_empty());
    }
}
