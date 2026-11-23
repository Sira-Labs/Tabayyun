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

/// The window's samples with `estimate` applied to every hole, and to every timestamp the
/// series misses inside the window when `grid` gives its interval.
///
/// A timestamp is missing where two neighbouring samples are more than one and a half
/// intervals apart; the missing ones continue the earlier sample's rhythm. The neighbours
/// may lie outside the window, but nothing is added before the first sample of the frame or
/// after its last.
fn fill(
    raw: &SeriesFrame,
    lo: usize,
    hi: usize,
    window: Window,
    grid: Option<i64>,
    estimate: impl Fn(i64) -> Option<f64>,
) -> Result<Patch> {
    let mut patch = Patch::default();
    let mut added = 0usize;
    let put = |patch: &mut Patch, t: i64, existing: Option<usize>| match (estimate(t), existing) {
        (Some(v), _) => patch.push(t, v, Quality::Estimated),
        (None, Some(i)) => {
            patch.push(t, raw.values[i], raw.quality[i]);
            patch.unrepaired += 1;
        }
        (None, None) => patch.unrepaired += 1,
    };
    // The timestamps missing between the samples at `from` and `to`, inside the window.
    let mut bridge = |patch: &mut Patch, from: i64, to: i64| -> Result<()> {
        let Some(grid) = grid else { return Ok(()) };
        if to.saturating_sub(from) <= grid.saturating_add(grid / 2) {
            return Ok(());
        }
        let first = from.saturating_add(grid).max(window.start);
        // Stay on the rhythm of `from` when the window starts inside the gap.
        let steps = first.saturating_sub(from).saturating_add(grid - 1) / grid;
        let mut t = from.saturating_add(steps.saturating_mul(grid));
        while t < window.end && to.saturating_sub(t) > grid / 2 {
            added += 1;
            if added > MAX_ADDED {
                return Err(invalid(format!("the operation would add more than {MAX_ADDED} samples")));
            }
            put(patch, t, None);
            let Some(next) = t.checked_add(grid) else { break };
            t = next;
        }
        Ok(())
    };
    let mut previous = lo.checked_sub(1).map(|i| raw.ts[i]);
    for i in lo..hi {
        if let Some(p) = previous {
            bridge(&mut patch, p, raw.ts[i])?;
        }
        if usable(raw.values[i], raw.quality[i]) {
            patch.push(raw.ts[i], raw.values[i], raw.quality[i]);
        } else {
            put(&mut patch, raw.ts[i], Some(i));
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
    fill(raw, lo, hi, window, interval(raw), |t| {
        let ((t0, v0), (t1, v1)) = known.around(t)?;
        let span = t1 as i128 - t0 as i128;
        if max_gap_ns.is_some_and(|g| span > g as i128) {
            return None;
        }
        let share = (t as i128 - t0 as i128) as f64 / span as f64;
        Some(v0 + (v1 - v0) * share).filter(|v| v.is_finite())
    })
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
    fill(raw, lo, hi, window, Some(grid), |t| {
        let reference = (1..=days as i64)
            .flat_map(|k| [t.checked_sub(k * step), t.checked_add(k * step)])
            .flatten()
            .filter_map(|at| known.near(at, grid / 2));
        let (sum, n) = reference.fold((0.0, 0usize), |(s, n), v| (s + v, n + 1));
        (n > 0).then(|| sum / n as f64).filter(|v| v.is_finite())
    })
}
