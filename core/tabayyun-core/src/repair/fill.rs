//! The imputing operations: they fill holes (samples without a usable, finite value) and
//! the timestamps a regular series is missing, and mark what they fill as estimated.

use super::{invalid, usable, Patch, MAX_ADDED};
use crate::error::Result;
use crate::finding::Window;
use crate::frame::SeriesFrame;
use crate::quality::Quality;
use crate::time::NS_PER_DAY;

/// The usable samples of the whole frame, in time order: what an estimate may build on.
struct Usable {
    ts: Vec<i64>,
    values: Vec<f64>,
}

impl Usable {
    fn of(raw: &SeriesFrame) -> Self {
        let keep: Vec<usize> = (0..raw.len()).filter(|&i| usable(raw.values[i], raw.quality[i])).collect();
        Self {
            ts: keep.iter().map(|&i| raw.ts[i]).collect(),
            values: keep.iter().map(|&i| raw.values[i]).collect(),
        }
    }

    /// The last usable sample before `t` and the first after it.
    fn around(&self, t: i64) -> Option<((i64, f64), (i64, f64))> {
        let right = self.ts.partition_point(|&u| u <= t);
        let left = self.ts.partition_point(|&u| u < t).checked_sub(1)?;
        (right < self.ts.len())
            .then(|| ((self.ts[left], self.values[left]), (self.ts[right], self.values[right])))
    }

    /// The value of the usable sample nearest to `t`, when it is within `tolerance`.
    fn near(&self, t: i64, tolerance: i64) -> Option<f64> {
        let at = self.ts.partition_point(|&u| u < t);
        [at.checked_sub(1), (at < self.ts.len()).then_some(at)]
            .into_iter()
            .flatten()
            .map(|i| (self.ts[i].abs_diff(t), self.values[i]))
            .filter(|(d, _)| *d <= tolerance as u64)
            .min_by_key(|(d, _)| *d)
            .map(|(_, v)| v)
    }
}

/// The series' sampling interval, when it has one.
fn interval(raw: &SeriesFrame) -> Option<i64> {
    raw.expected_interval_ns().filter(|g| *g > 0)
}

/// A stretch of time, both ends included, in which no missing timestamp can be estimated.
type Hopeless = Option<(i128, i128)>;

/// The window's samples with `estimate` applied to every hole, and to every timestamp the
/// series misses inside the window when `grid` gives its interval.
///
/// A timestamp is missing where two neighbouring samples are more than one and a half
/// intervals apart; the missing ones continue the earlier sample's rhythm. The neighbours
/// may lie outside the window, but nothing is added before the first sample of the frame or
/// after its last.
///
/// `hopeless` names, for the gap between two neighbouring samples, the stretch in which
/// `estimate` cannot succeed. Its missing timestamps are counted as unrepaired without being
/// visited, so a gap of years costs nothing when nothing in it can be estimated.
fn fill(
    raw: &SeriesFrame,
    lo: usize,
    hi: usize,
    window: Window,
    grid: Option<i64>,
    estimate: impl Fn(i64) -> Option<f64>,
    hopeless: impl Fn(i64, i64) -> Hopeless,
) -> Result<Patch> {
    let mut patch = Patch::default();
    let mut visited = 0usize;
    // The timestamps missing between the samples at `from` and `to`, inside the window. The
    // arithmetic is in i128: the samples may be the whole range of i64 apart.
    let mut bridge = |patch: &mut Patch, from: i64, to: i64| -> Result<()> {
        let Some(grid) = grid.map(i128::from) else { return Ok(()) };
        let (start, end) = (i128::from(from), i128::from(to));
        if end - start <= grid + grid / 2 {
            return Ok(());
        }
        // On the rhythm of `from`, and not before the window when it starts inside the gap.
        let first = (start + grid).max(i128::from(window.start));
        let mut t = start + (first - start + grid - 1) / grid * grid;
        // Inside the window, and more than half an interval before the next sample.
        let last = (i128::from(window.end) - 1).min(end - grid / 2 - 1);
        let dead = hopeless(from, to);
        while t <= last {
            if let Some((_, dead_end)) = dead.filter(|(a, b)| (*a..=*b).contains(&t)) {
                let skipped = (dead_end.min(last) - t) / grid + 1;
                patch.unrepaired =
                    patch.unrepaired.saturating_add(usize::try_from(skipped).unwrap_or(usize::MAX));
                t += skipped * grid;
                continue;
            }
            visited += 1;
            if visited > MAX_ADDED {
                return Err(invalid(format!(
                    "the operation would estimate more than {MAX_ADDED} missing timestamps"
                )));
            }
            // `t` lies inside the window, so it is a timestamp.
            let at = t as i64;
            match estimate(at) {
                Some(v) => patch.push(at, v, Quality::Estimated),
                None => patch.unrepaired += 1,
            }
            t += grid;
        }
        Ok(())
    };
    let mut previous = lo.checked_sub(1).map(|i| raw.ts[i]);
    for i in lo..hi {
        if let Some(p) = previous {
            bridge(&mut patch, p, raw.ts[i])?;
        }
        let hole = !usable(raw.values[i], raw.quality[i]);
        match hole.then(|| estimate(raw.ts[i])).flatten() {
            Some(v) => patch.push(raw.ts[i], v, Quality::Estimated),
            None => {
                patch.push(raw.ts[i], raw.values[i], raw.quality[i]);
                patch.unrepaired += usize::from(hole);
            }
        }
        previous = Some(raw.ts[i]);
    }
    if let (Some(p), Some(&next)) = (previous, raw.ts.get(hi)) {
        bridge(&mut patch, p, next)?;
    }
    Ok(patch)
}

pub(super) fn linear(
    raw: &SeriesFrame,
    lo: usize,
    hi: usize,
    window: Window,
    max_gap_ns: Option<i64>,
) -> Result<Patch> {
    if max_gap_ns.is_some_and(|g| g <= 0) {
        return Err(invalid("impute.linear: max_gap_ns must be positive"));
    }
    let known = Usable::of(raw);
    // The usable samples around `t`, when there are two and they are close enough.
    let around = |t: i64| {
        known
            .around(t)
            .filter(|((t0, _), (t1, _))| max_gap_ns.is_none_or(|g| *t1 as i128 - *t0 as i128 <= g as i128))
    };
    let estimate = |t: i64| {
        let ((t0, v0), (t1, v1)) = around(t)?;
        let share = (t as i128 - t0 as i128) as f64 / (t1 as i128 - t0 as i128) as f64;
        Some(v0 + (v1 - v0) * share).filter(|v| v.is_finite())
    };
    // Between two neighbouring samples the usable samples around are the same everywhere:
    // without them, or with them too far apart, the whole gap is hopeless.
    let hopeless = |from: i64, to: i64| -> Hopeless {
        around(from.saturating_add(1)).is_none().then(|| (i128::from(from) + 1, i128::from(to) - 1))
    };
    fill(raw, lo, hi, window, interval(raw), estimate, hopeless)
}

pub(super) fn like_day(
    raw: &SeriesFrame,
    lo: usize,
    hi: usize,
    window: Window,
    days: usize,
    same_weekday: bool,
) -> Result<Patch> {
    if !(1..=60).contains(&days) {
        return Err(invalid("impute.like_day: days must be 1 to 60"));
    }
    let Some(grid) = interval(raw) else {
        return Err(invalid("impute.like_day needs the series' sampling interval"));
    };
    let step = if same_weekday { 7 * NS_PER_DAY } else { NS_PER_DAY };
    let known = Usable::of(raw);
    let estimate = |t: i64| {
        let reference = (1..=days as i64)
            .flat_map(|k| [t.checked_sub(k * step), t.checked_add(k * step)])
            .flatten()
            .filter_map(|at| known.near(at, grid / 2));
        let (sum, n) = reference.fold((0.0, 0usize), |(s, n), v| (s + v, n + 1));
        (n > 0).then(|| sum / n as f64).filter(|v| v.is_finite())
    };
    // A reference is a usable sample at most `days` steps and half an interval away, and a
    // gap has none inside: its middle, farther than that from both ends, is hopeless.
    let reach = i128::from(days as i64 * step) + i128::from(grid / 2);
    let hopeless = |from: i64, to: i64| -> Hopeless {
        let (a, b) = (i128::from(from) + reach + 1, i128::from(to) - reach - 1);
        (a <= b).then_some((a, b))
    };
    fill(raw, lo, hi, window, Some(grid), estimate, hopeless)
}
