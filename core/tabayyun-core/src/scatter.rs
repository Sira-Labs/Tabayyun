//! One series against another on the checks' grid, for the compare view's scatter (spec 033).
//!
//! The two series are put on one grid by [`align`], exactly as `tby.correlation_break` and
//! the residual lane see them. Every grid bin in which both have a value is one point
//! `(x, y)` with the bin's time. [`scatter`] also gives the Pearson correlation over all
//! those bins and caps the points it returns, so a year of minutes does not travel to the
//! browser.

use crate::align::{align, grid_for};
use crate::error::{Error, Result};
use crate::frame::SeriesFrame;
use crate::residual::windowed;

/// Points of one series against another. `ts`, `x` and `y` have one entry per point, in time
/// order.
#[derive(Debug, Clone, PartialEq)]
pub struct Scatter {
    /// Grid step; 0 when the series give no grid.
    pub grid_ns: i64,
    /// Grid bins in which both series have a value, before the cap.
    pub n_bins: usize,
    /// Pearson correlation over all those bins; `None` with fewer than 3 bins or when a
    /// series is constant.
    pub r: Option<f64>,
    /// Start of each point's bin, clamped to the window start.
    pub ts: Vec<i64>,
    pub x: Vec<f64>,
    pub y: Vec<f64>,
    /// Ids of the series without a usable, finite sample in the window. When this is not
    /// empty there are no points.
    pub missing: Vec<String>,
}

fn invalid(message: &str) -> Error {
    Error::InvalidParams { check: "scatter".into(), reason: message.into() }
}

/// Pearson correlation of two equally long slices, or `None` when it is not defined.
fn pearson(x: &[f64], y: &[f64]) -> Option<f64> {
    let n = x.len();
    if n < 3 {
        return None;
    }
    let mean = |v: &[f64]| v.iter().sum::<f64>() / n as f64;
    let (mx, my) = (mean(x), mean(y));
    let (mut sxy, mut sxx, mut syy) = (0.0, 0.0, 0.0);
    for i in 0..n {
        let (dx, dy) = (x[i] - mx, y[i] - my);
        sxy += dx * dy;
        sxx += dx * dx;
        syy += dy * dy;
    }
    let r = sxy / (sxx * syy).sqrt();
    (sxx > 0.0 && syy > 0.0 && r.is_finite()).then(|| r.clamp(-1.0, 1.0))
}

/// Largest minus smallest value, or 1 when that is zero or not finite, so it can divide.
fn range_of(v: &[f64]) -> f64 {
    let lo = v.iter().copied().fold(f64::INFINITY, f64::min);
    let hi = v.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    Some(hi - lo).filter(|d| d.is_finite() && *d > 0.0).unwrap_or(1.0)
}

/// Indices of at most `max_points` of `n` points, ascending.
///
/// The points are cut into consecutive chunks of equal length. Each chunk gives its middle
/// point, which keeps the cloud's density over time, and the point farthest from the chunk's
/// centre in units of the x and y ranges, which keeps what leaves the cloud.
fn capped(x: &[f64], y: &[f64], max_points: usize) -> Vec<usize> {
    let n = x.len();
    if n <= max_points {
        return (0..n).collect();
    }
    let chunk = n.div_ceil((max_points / 2).max(1));
    let (xr, yr) = (range_of(x), range_of(y));
    let mut keep = Vec::with_capacity(max_points);
    for start in (0..n).step_by(chunk) {
        let end = (start + chunk).min(n);
        let len = (end - start) as f64;
        let cx = x[start..end].iter().sum::<f64>() / len;
        let cy = y[start..end].iter().sum::<f64>() / len;
        let distance = |i: usize| ((x[i] - cx) / xr).powi(2) + ((y[i] - cy) / yr).powi(2);
        let mid = start + (end - start) / 2;
        let far = (start..end).fold(start, |best, i| if distance(i) > distance(best) { i } else { best });
        keep.push(mid.min(far));
        if far != mid && max_points > 1 {
            keep.push(mid.max(far));
        }
    }
    keep
}

/// `y` against `x` over `[from, to)`, at most `max_points` points.
///
/// The grid is `grid_ns` when positive, else the coarser expected interval of the two (see
/// [`grid_for`]). Both frames must be sorted by time.
pub fn scatter(
    x: &SeriesFrame,
    y: &SeriesFrame,
    grid_ns: Option<i64>,
    from: i64,
    to: i64,
    max_points: usize,
) -> Result<Scatter> {
    if to <= from {
        return Err(invalid("the window start must be before its end"));
    }
    if max_points == 0 {
        return Err(invalid("needs max_points > 0"));
    }
    if !x.is_sorted() || !y.is_sorted() {
        return Err(invalid("frames must be sorted by time"));
    }
    let cut = [windowed(x, from, to)?, windowed(y, from, to)?];
    let frames = [&cut[0], &cut[1]];
    let missing: Vec<String> = frames
        .iter()
        .filter(|f| !(0..f.len()).any(|i| f.values[i].is_finite() && f.quality[i].is_usable()))
        .map(|f| f.meta.id.clone())
        .collect();
    let grid = grid_for(&[x, y], grid_ns);
    let mut out = Scatter {
        grid_ns: grid.unwrap_or(0),
        n_bins: 0,
        r: None,
        ts: Vec::new(),
        x: Vec::new(),
        y: Vec::new(),
        missing,
    };
    if !out.missing.is_empty() {
        return Ok(out);
    }
    let aligned = align(&frames, grid);
    let complete: Vec<usize> = (0..aligned.len()).filter(|&i| aligned.complete(i)).collect();
    let xs: Vec<f64> = complete.iter().map(|&i| aligned.columns[0][i]).collect();
    let ys: Vec<f64> = complete.iter().map(|&i| aligned.columns[1][i]).collect();
    out.n_bins = complete.len();
    out.r = pearson(&xs, &ys);
    for k in capped(&xs, &ys, max_points) {
        out.ts.push(aligned.ts[complete[k]].max(from));
        out.x.push(xs[k]);
        out.y.push(ys[k]);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::frame::SeriesMeta;
    use crate::quality::Quality;

    const MIN: i64 = 60_000_000_000;

    fn frame(id: &str, values: &[f64]) -> SeriesFrame {
        let ts = (0..values.len() as i64).map(|i| i * MIN).collect();
        SeriesFrame::with_default_quality(SeriesMeta::new(id), ts, values.to_vec()).unwrap()
    }

    #[test]
    fn one_point_per_complete_bin() {
        let a = frame("a", &[1.0, 2.0, 3.0, 4.0, 5.0]);
        let mut b = frame("b", &[10.0, 20.0, 30.0, 40.0, 50.0]);
        b.values[1] = f64::NAN;
        b.quality[3] = Quality::Bad;
        let s = scatter(&a, &b, None, 0, 5 * MIN, 100).unwrap();
        assert_eq!((s.grid_ns, s.n_bins), (MIN, 3));
        assert_eq!(s.ts, vec![0, 2 * MIN, 4 * MIN]);
        assert_eq!((s.x, s.y), (vec![1.0, 3.0, 5.0], vec![10.0, 30.0, 50.0]));
        assert!(s.missing.is_empty());
    }

    #[test]
    fn pearson_over_all_bins() {
        let x: Vec<f64> = (0..1000).map(f64::from).collect();
        let a = frame("a", &x);
        let up = frame("up", &x.iter().map(|v| 2.0 * v + 1.0).collect::<Vec<_>>());
        let down = frame("down", &x.iter().map(|v| -v).collect::<Vec<_>>());
        let flat = frame("flat", &[7.0; 1000]);
        let to = 1000 * MIN;
        // The correlation is over the 1000 bins, whatever the cap.
        let capped = scatter(&a, &up, None, 0, to, 10).unwrap();
        assert!((capped.r.unwrap() - 1.0).abs() < 1e-12);
        assert_eq!((capped.n_bins, capped.ts.len() <= 10), (1000, true));
        assert!((scatter(&a, &down, None, 0, to, 10).unwrap().r.unwrap() + 1.0).abs() < 1e-12);
        assert_eq!(scatter(&a, &flat, None, 0, to, 10).unwrap().r, None);
        assert_eq!(scatter(&a, &up, None, 0, 2 * MIN, 10).unwrap().r, None);
    }

    #[test]
    fn cap_keeps_a_short_excursion() {
        // 10 000 bins on the line y = x, and three bins far off it: a stride of 100 would
        // miss them, the farthest point of their chunk does not.
        let n = 10_000;
        let x: Vec<f64> = (0..n).map(|i| f64::from(i % 100)).collect();
        let mut y = x.clone();
        for v in &mut y[4321..4324] {
            *v += 500.0;
        }
        let s = scatter(&frame("a", &x), &frame("b", &y), None, 0, n as i64 * MIN, 200).unwrap();
        assert_eq!(s.n_bins, n as usize);
        assert!(s.ts.len() <= 200 && s.ts.len() > 100, "{}", s.ts.len());
        let off: Vec<i64> = (0..s.ts.len()).filter(|&i| s.y[i] - s.x[i] > 400.0).map(|i| s.ts[i]).collect();
        assert_eq!(off.len(), 1);
        assert!((4321 * MIN..4324 * MIN).contains(&off[0]));
    }

    #[test]
    fn cap_is_deterministic_and_ordered() {
        let x: Vec<f64> = (0..5000).map(|i| f64::from(i * 7919 % 101)).collect();
        let y: Vec<f64> = (0..5000).map(|i| f64::from(i * 104_729 % 97)).collect();
        let (a, b) = (frame("a", &x), frame("b", &y));
        for max in [1, 2, 3, 99, 100, 2000, 4999, 5000] {
            let s = scatter(&a, &b, None, 0, 5000 * MIN, max).unwrap();
            assert!(s.ts.len() <= max, "max {max}: {}", s.ts.len());
            assert!(s.ts.windows(2).all(|w| w[0] < w[1]), "max {max}");
            assert_eq!(s, scatter(&a, &b, None, 0, 5000 * MIN, max).unwrap());
        }
        assert_eq!(scatter(&a, &b, None, 0, 5000 * MIN, 5000).unwrap().ts.len(), 5000);
    }

    #[test]
    fn missing_series_reported() {
        let a = frame("a", &[1.0, 2.0, 3.0]);
        let mut b = frame("b", &[1.0, f64::NAN, 3.0]);
        b.quality = vec![Quality::Bad, Quality::Good, Quality::Uncertain];
        let s = scatter(&a, &b, None, 0, 3 * MIN, 10).unwrap();
        assert_eq!(s.missing, vec!["b".to_string()]);
        assert!(s.ts.is_empty() && s.n_bins == 0 && s.r.is_none());
        // Samples outside the window do not count either.
        let s = scatter(&a, &a, None, 10 * MIN, 20 * MIN, 10).unwrap();
        assert_eq!(s.missing, vec!["a".to_string(), "a".to_string()]);
    }

    #[test]
    fn window_and_grid() {
        let a = frame("a", &[1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]);
        let b = frame("b", &[0.0; 10]);
        let s = scatter(&a, &b, None, MIN, 3 * MIN, 10).unwrap();
        assert_eq!((s.ts, s.x), (vec![MIN, 2 * MIN], vec![2.0, 3.0]));
        // A 5-minute grid and a window from minute 2: the first bin holds minutes 2 to 4 and
        // is drawn at the window start.
        let s = scatter(&a, &b, Some(5 * MIN), 2 * MIN, 10 * MIN, 10).unwrap();
        assert_eq!((s.grid_ns, s.ts), (5 * MIN, vec![2 * MIN, 5 * MIN]));
        assert_eq!(s.x, vec![4.0, 8.0]);
    }

    #[test]
    fn invalid_arguments_are_errors() {
        let a = frame("a", &[1.0, 2.0]);
        assert!(scatter(&a, &a, None, MIN, MIN, 10).is_err());
        assert!(scatter(&a, &a, None, 0, MIN, 0).is_err());
        let unsorted =
            SeriesFrame::with_default_quality(SeriesMeta::new("u"), vec![MIN, 0], vec![1.0, 2.0]).unwrap();
        assert!(scatter(&a, &unsorted, None, 0, 2 * MIN, 10).is_err());
    }
}
