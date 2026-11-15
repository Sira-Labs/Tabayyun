//! Residual of a balance or a pair over a window, for the compare view (spec 018).
//!
//! The members are put on one grid by [`align`], exactly as the cross-series checks see them:
//! a member's value in a grid bin is the mean of its usable, finite samples there. The
//! residual is the signed sum of the members (inputs minus outputs for a balance, A − B for a
//! pair) and exists only in bins where every member has a value, as in
//! `tby.balance_residual`. Incomplete bins are skipped and do not suppress the others.
//!
//! [`residual`] works on the grid; [`residual_chart`] reduces its result to chart points on
//! the bins of the window with [`m4_window`], so the lane shares bin edges with the series
//! above it.

use crate::align::{align, grid_for};
use crate::downsample::{gap_breaks, m4_window};
use crate::error::{Error, Result};
use crate::frame::{SeriesFrame, SeriesMeta};

/// The residual of a group on its grid. `ts`, `residual` and `sum_in` have one entry per
/// complete grid bin, in time order.
#[derive(Debug, Clone, PartialEq)]
pub struct Residual {
    /// Grid step; 0 when the members give no grid.
    pub grid_ns: i64,
    /// Start of each complete bin, clamped to the window start.
    pub ts: Vec<i64>,
    /// Signed sum of the members in the bin.
    pub residual: Vec<f64>,
    /// Sum of the members with a positive sign (a balance's inputs), which scales the loss band.
    pub sum_in: Vec<f64>,
    /// Ids of members without a usable, finite sample in the window. When this is not empty
    /// there is no residual.
    pub missing: Vec<String>,
}

/// Chart points of a residual: the residual and, for a balance, the loss band at each point.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct ResidualChart {
    pub ts: Vec<i64>,
    /// NaN where the line breaks across skipped bins.
    pub residual: Vec<f64>,
    /// Lower and upper edge of the loss band; empty without a band.
    pub lo: Vec<f64>,
    pub hi: Vec<f64>,
}

fn invalid(message: &str) -> Error {
    Error::InvalidParams { check: "residual".into(), reason: message.into() }
}

/// The samples of a sorted frame inside `[from, to)` as a frame of their own.
fn windowed(frame: &SeriesFrame, from: i64, to: i64) -> Result<SeriesFrame> {
    let lo = frame.ts.partition_point(|&t| t < from);
    let hi = frame.ts.partition_point(|&t| t < to).max(lo);
    SeriesFrame::new(
        frame.meta.clone(),
        frame.ts[lo..hi].to_vec(),
        frame.values[lo..hi].to_vec(),
        frame.quality[lo..hi].to_vec(),
    )
}

/// Residual `Σ signs[j] · member[j]` of sorted `frames` over `[from, to)`.
///
/// `signs` has one entry per frame: `+1` for a balance's inputs and a pair's first member,
/// `−1` for outputs and the second member. The grid is `grid_ns` when positive, else the
/// coarsest expected interval among the members (see [`grid_for`]).
pub fn residual(
    frames: &[&SeriesFrame],
    signs: &[f64],
    grid_ns: Option<i64>,
    from: i64,
    to: i64,
) -> Result<Residual> {
    if frames.len() < 2 || frames.len() != signs.len() {
        return Err(invalid("needs at least two members and one sign per member"));
    }
    if signs.iter().any(|s| !s.is_finite() || *s == 0.0) {
        return Err(invalid("signs must be finite and not zero"));
    }
    if to <= from {
        return Err(invalid("the window start must be before its end"));
    }
    if frames.iter().any(|f| !f.is_sorted()) {
        return Err(invalid("frames must be sorted by time"));
    }
    let grid = grid_for(frames, grid_ns);
    let cut: Vec<SeriesFrame> = frames.iter().map(|f| windowed(f, from, to)).collect::<Result<_>>()?;
    let missing: Vec<String> = cut
        .iter()
        .filter(|f| !(0..f.len()).any(|i| f.values[i].is_finite() && f.quality[i].is_usable()))
        .map(|f| f.meta.id.clone())
        .collect();
    let mut out = Residual {
        grid_ns: grid.unwrap_or(0),
        ts: Vec::new(),
        residual: Vec::new(),
        sum_in: Vec::new(),
        missing,
    };
    if !out.missing.is_empty() {
        return Ok(out);
    }
    let aligned = align(&cut.iter().collect::<Vec<_>>(), grid);
    for i in (0..aligned.len()).filter(|&i| aligned.complete(i)) {
        let member = |j: usize| aligned.columns[j][i];
        out.ts.push(aligned.ts[i].max(from));
        out.residual.push((0..signs.len()).map(|j| signs[j] * member(j)).sum());
        out.sum_in.push((0..signs.len()).filter(|&j| signs[j] > 0.0).map(member).sum());
    }
    Ok(out)
}

/// Loss band of one grid bin: `[loss_min · Σin, loss_max · Σin]`, collapsing to r = 0 without
/// input, as in `tby.balance_residual`.
fn band_of(sum_in: f64, loss: (f64, f64)) -> (f64, f64) {
    if sum_in > 0.0 {
        (loss.0 * sum_in, loss.1 * sum_in)
    } else {
        (0.0, 0.0)
    }
}

/// Chart points of `r` on `buckets` equal bins of `[from, to)`, with the loss band when `loss`
/// gives `(loss_min, loss_max)`.
///
/// The residual is M4-downsampled like a series, and every point that is kept carries the
/// band of its own grid bin. A point drawn outside the band was therefore outside it in its
/// grid bin, and a point drawn inside was inside. Grid bins that M4 drops are not drawn at
/// all: M4 keeps each chart bin's lowest and highest residual, so an excursion is lost only
/// when the band itself moves inside one chart bin by more than the residual does. The line
/// breaks where grid bins were skipped.
pub fn residual_chart(
    r: &Residual,
    from: i64,
    to: i64,
    buckets: usize,
    loss: Option<(f64, f64)>,
) -> Result<ResidualChart> {
    if buckets == 0 || to <= from {
        return Err(invalid("needs buckets > 0 and a window start before its end"));
    }
    if r.ts.is_empty() {
        return Ok(ResidualChart::default());
    }
    let frame = SeriesFrame::with_default_quality(SeriesMeta::new("_"), r.ts.clone(), r.residual.clone())?;
    let (ts, values) = m4_window(&frame, from, to, buckets);
    let pixel = ((to as i128 - from as i128) / buckets as i128).max(1) as i64;
    let max_step = pixel.saturating_mul(2).max(r.grid_ns);
    let (out_ts, residual) = gap_breaks(&ts, &values, max_step);
    let Some(loss) = loss else {
        return Ok(ResidualChart { ts: out_ts, residual, ..ResidualChart::default() });
    };
    // The band breaks exactly where the residual does, whatever the band's own values are
    // (an overflowing input makes an edge NaN), so the arrays always have the same length.
    let breaks_after =
        |i: usize| i + 1 < ts.len() && ts[i + 1].saturating_sub(ts[i]) > max_step && !values[i].is_nan();
    let (mut lo, mut hi) = (Vec::with_capacity(out_ts.len()), Vec::with_capacity(out_ts.len()));
    for (i, t) in ts.iter().enumerate() {
        // `ts` is a subset of `r.ts`, which is strictly increasing, so the search always hits.
        let (l, h) = r.ts.binary_search(t).map_or((f64::NAN, f64::NAN), |k| band_of(r.sum_in[k], loss));
        lo.push(l);
        hi.push(h);
        if breaks_after(i) {
            lo.push(f64::NAN);
            hi.push(f64::NAN);
        }
    }
    Ok(ResidualChart { ts: out_ts, residual, lo, hi })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::quality::Quality;

    const MIN: i64 = 60_000_000_000;
    const H: usize = 60;

    fn frame(id: &str, values: &[f64]) -> SeriesFrame {
        let ts = (0..values.len() as i64).map(|i| i * MIN).collect();
        SeriesFrame::with_default_quality(SeriesMeta::new(id), ts, values.to_vec()).unwrap()
    }

    /// One inlet of 300 and three outlets of 98 each (2 % loss); outlet `c` reads 30 % low
    /// between hours 6 and 12.
    fn balance() -> [SeriesFrame; 4] {
        let n = 24 * H;
        let c: Vec<f64> = (0..n).map(|i| if (6 * H..12 * H).contains(&i) { 68.6 } else { 98.0 }).collect();
        [frame("in", &vec![300.0; n]), frame("a", &vec![98.0; n]), frame("b", &vec![98.0; n]), frame("c", &c)]
    }

    const SIGNS: [f64; 4] = [1.0, -1.0, -1.0, -1.0];

    #[test]
    fn balance_residual_matches_sum() {
        let f = balance();
        let r = residual(&f.iter().collect::<Vec<_>>(), &SIGNS, None, 0, 24 * 60 * MIN).unwrap();
        assert_eq!(r.grid_ns, MIN);
        assert_eq!(r.ts.len(), 24 * H);
        assert!(r.missing.is_empty());
        for i in 0..r.ts.len() {
            let expected = if (6 * H..12 * H).contains(&i) { 300.0 - 98.0 - 98.0 - 68.6 } else { 6.0 };
            assert!((r.residual[i] - expected).abs() < 1e-9, "bin {i}: {}", r.residual[i]);
            assert_eq!(r.sum_in[i], 300.0);
        }
        // With the default band of −1 % to 5 % (−3 to 15) the residual leaves it exactly while
        // `c` reads low.
        let chart = residual_chart(&r, 0, 24 * 60 * MIN, 2000, Some((-0.01, 0.05))).unwrap();
        assert_eq!(chart.ts, r.ts);
        for i in 0..chart.ts.len() {
            let outside = chart.residual[i] > chart.hi[i] || chart.residual[i] < chart.lo[i];
            assert_eq!(outside, (6 * H..12 * H).contains(&i), "bin {i}");
            assert_eq!((chart.lo[i], chart.hi[i]), (-3.0, 15.0));
        }
    }

    #[test]
    fn pair_difference() {
        let a = frame("a", &[10.0, 11.0, 12.0]);
        let b = frame("b", &[9.0, 11.5, 10.0]);
        let r = residual(&[&a, &b], &[1.0, -1.0], None, 0, 3 * MIN).unwrap();
        assert_eq!(r.residual, vec![1.0, -0.5, 2.0]);
        assert_eq!(r.sum_in, vec![10.0, 11.0, 12.0]);
        let chart = residual_chart(&r, 0, 3 * MIN, 100, None).unwrap();
        assert_eq!(chart.residual, vec![1.0, -0.5, 2.0]);
        assert!(chart.lo.is_empty() && chart.hi.is_empty());
    }

    #[test]
    fn missing_member_reported() {
        let a = frame("a", &[1.0, 2.0, 3.0]);
        // `b` has samples in the window, but none is usable; `c` has none in the window at all.
        let mut b = frame("b", &[1.0, f64::NAN, 3.0]);
        b.quality = vec![Quality::Bad, Quality::Good, Quality::Uncertain];
        let c = SeriesFrame::with_default_quality(SeriesMeta::new("c"), vec![10 * MIN], vec![1.0]).unwrap();
        let r = residual(&[&a, &b, &c], &[1.0, -1.0, -1.0], None, 0, 3 * MIN).unwrap();
        assert_eq!(r.missing, vec!["b".to_string(), "c".to_string()]);
        assert!(r.ts.is_empty() && r.residual.is_empty());
        assert_eq!(residual_chart(&r, 0, 3 * MIN, 10, Some((0.0, 0.1))).unwrap(), ResidualChart::default());
    }

    #[test]
    fn incomplete_bins_are_skipped_and_break_the_line() {
        let a = frame("a", &[5.0; 10]);
        let mut b = frame("b", &[3.0; 10]);
        b.values[4] = f64::NAN; // no value
        b.quality[5] = Quality::Bad; // a value that does not count
        let r = residual(&[&a, &b], &[1.0, -1.0], None, 0, 10 * MIN).unwrap();
        assert_eq!(r.ts, [0, 1, 2, 3, 6, 7, 8, 9].map(|i| i * MIN).to_vec());
        assert!(r.residual.iter().all(|&x| x == 2.0));
        let chart = residual_chart(&r, 0, 10 * MIN, 1000, None).unwrap();
        assert_eq!(chart.ts.len(), 9);
        assert_eq!(chart.ts[4], 3 * MIN + 1);
        assert!(chart.residual[4].is_nan());
    }

    #[test]
    fn rows_outside_the_window_do_not_count() {
        let a = frame("a", &[1.0, 2.0, 3.0, 4.0]);
        let b = frame("b", &[0.0, 0.0, 0.0, 0.0]);
        let r = residual(&[&a, &b], &[1.0, -1.0], None, MIN, 3 * MIN).unwrap();
        assert_eq!(r.ts, vec![MIN, 2 * MIN]);
        assert_eq!(r.residual, vec![2.0, 3.0]);
    }

    #[test]
    fn a_bin_that_starts_before_the_window_is_drawn_at_its_start() {
        // A 5-minute grid and a window starting at minute 2: the first bin starts at 0.
        let a = frame("a", &[1.0; 10]);
        let b = frame("b", &[0.5; 10]);
        let r = residual(&[&a, &b], &[1.0, -1.0], Some(5 * MIN), 2 * MIN, 10 * MIN).unwrap();
        assert_eq!(r.ts, vec![2 * MIN, 5 * MIN]);
        let chart = residual_chart(&r, 2 * MIN, 10 * MIN, 100, None).unwrap();
        assert_eq!(chart.ts, r.ts);
    }

    #[test]
    fn downsampled_points_carry_the_band_of_their_own_grid_bin() {
        // The input steps from 100 to 200 inside the fifth of ten chart bins, and the residual
        // is 8 throughout: outside the band of 5 before the step, inside the band of 10 after
        // it. A band widened over the chart bin would hide the violation there.
        let n = 2000;
        let step = 900;
        let input: Vec<f64> = (0..n).map(|i| if i < step { 100.0 } else { 200.0 }).collect();
        let a = frame("in", &input);
        let b = frame("out", &input.iter().map(|x| x - 8.0).collect::<Vec<_>>());
        let to = n as i64 * MIN;
        let r = residual(&[&a, &b], &[1.0, -1.0], None, 0, to).unwrap();
        let chart = residual_chart(&r, 0, to, 10, Some((-0.01, 0.05))).unwrap();
        assert!(chart.ts.len() <= 40 && chart.ts.len() < n);
        assert_eq!(chart.ts.len(), chart.lo.len());
        assert_eq!(chart.ts.len(), chart.hi.len());
        let mut in_step_bin = (0, 0);
        for i in 0..chart.ts.len() {
            let before = chart.ts[i] < step as i64 * MIN;
            assert_eq!(chart.lo[i], if before { -1.0 } else { -2.0 }, "point {i}");
            assert_eq!(chart.hi[i], if before { 5.0 } else { 10.0 }, "point {i}");
            assert_eq!(chart.residual[i] > chart.hi[i], before, "point {i}");
            if (800 * MIN..1000 * MIN).contains(&chart.ts[i]) {
                if before {
                    in_step_bin.0 += 1
                } else {
                    in_step_bin.1 += 1
                }
            }
        }
        // The chart bin of the step shows both: a point outside its band and one inside.
        assert!(in_step_bin.0 >= 1 && in_step_bin.1 >= 1, "{in_step_bin:?}");
    }

    #[test]
    fn band_arrays_keep_the_residuals_length_when_an_edge_is_not_finite() {
        // An input sum that overflowed makes the band's lower edge NaN (0 × inf) at the first
        // point; the gap after it must still be inserted in every array.
        let r = Residual {
            grid_ns: 1,
            ts: vec![0, 4],
            residual: vec![0.0, 0.0],
            sum_in: vec![f64::INFINITY, 2.0],
            missing: Vec::new(),
        };
        let chart = residual_chart(&r, 0, 5, 100, Some((0.0, 0.05))).unwrap();
        assert_eq!(chart.ts, vec![0, 1, 4]);
        assert_eq!((chart.residual.len(), chart.lo.len(), chart.hi.len()), (3, 3, 3));
        assert!(chart.lo[0].is_nan() && chart.lo[1].is_nan() && chart.hi[1].is_nan());
        assert_eq!((chart.lo[2], chart.hi[2]), (0.0, 0.1));
        assert!(chart.residual[1].is_nan());
    }

    #[test]
    fn invalid_arguments_are_errors() {
        let a = frame("a", &[1.0]);
        let b = frame("b", &[1.0]);
        assert!(residual(&[&a], &[1.0], None, 0, MIN).is_err());
        assert!(residual(&[&a, &b], &[1.0], None, 0, MIN).is_err());
        assert!(residual(&[&a, &b], &[1.0, 0.0], None, 0, MIN).is_err());
        assert!(residual(&[&a, &b], &[1.0, -1.0], None, MIN, MIN).is_err());
        let r = residual(&[&a, &b], &[1.0, -1.0], None, 0, MIN).unwrap();
        assert!(residual_chart(&r, 0, MIN, 0, None).is_err());
    }
}
