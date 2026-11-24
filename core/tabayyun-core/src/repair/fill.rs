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
///
/// `hope` answers, for a time inside a gap, with the first time from there on at which
/// `estimate` can succeed, or `None` when it cannot anywhere in the rest of the gap. The
/// missing timestamps before that time are counted as unrepaired without being visited, so
/// a gap of years costs only what can be filled in it.
fn fill(
    raw: &SeriesFrame,
    lo: usize,
    hi: usize,
    window: Window,
    grid: Option<i64>,
    estimate: impl Fn(i64) -> Option<f64>,
    hope: impl Fn(i64) -> Option<i64>,
) -> Result<Patch> {
    let mut patch = Patch::default();
    let mut added = 0usize;
    let count = |n: i128| usize::try_from(n).unwrap_or(usize::MAX);
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
        while t <= last {
            // `t` lies inside the window, so it is a timestamp.
            let at = t as i64;
            let Some(next) = hope(at).map(i128::from).filter(|h| *h <= last) else {
                patch.unrepaired = patch.unrepaired.saturating_add(count((last - t) / grid + 1));
                break;
            };
            if next > t {
                let skipped = (next - t + grid - 1) / grid;
                patch.unrepaired = patch.unrepaired.saturating_add(count(skipped));
                t += skipped * grid;
                continue;
            }
            match estimate(at) {
                Some(v) => {
                    added += 1;
                    if added > MAX_ADDED {
                        return Err(invalid(format!(
                            "the operation would add more than {MAX_ADDED} samples"
                        )));
                    }
                    patch.push(at, v, Quality::Estimated);
                }
                None => patch.unrepaired = patch.unrepaired.saturating_add(1),
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
                patch.unrepaired = patch.unrepaired.saturating_add(usize::from(hole));
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
        // The difference of two huge values of opposite sign is not a number any more; the
        // weighted form of the same line always is.
        let direct = v0 + (v1 - v0) * share;
        Some(if direct.is_finite() { direct } else { v0 * (1.0 - share) + v1 * share })
    };
    // Between two neighbouring samples the usable samples around are the same everywhere:
    // either every missing timestamp of the gap can be estimated, or none. `estimate` gives
    // a value whenever `around` does, so `hope` promises nothing it cannot keep.
    let hope = |t: i64| around(t).map(|_| t);
    fill(raw, lo, hi, window, interval(raw), estimate, hope)
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
    let tolerance = i128::from(grid / 2);
    let range = i128::from(i64::MIN)..=i128::from(i64::MAX);
    // How far away, in time, the references of a timestamp lie.
    let shifts = || (1..=days as i128).flat_map(|k| [-k * i128::from(step), k * i128::from(step)]);
    let estimate = |t: i64| {
        let found: Vec<f64> = shifts()
            .filter_map(|shift| i64::try_from(i128::from(t) + shift).ok())
            .filter_map(|at| known.near(at, grid / 2))
            .collect();
        // Shares are summed so that no partial sum leaves the numbers; only the last step
        // can round past the largest value (three times a third of it), and a mean lies
        // between its smallest and largest reference, so it is held there.
        let n = found.len() as f64;
        let least = found.iter().copied().fold(f64::INFINITY, f64::min);
        let most = found.iter().copied().fold(f64::NEG_INFINITY, f64::max);
        (!found.is_empty()).then(|| found.iter().map(|v| v / n).sum::<f64>().clamp(least, most))
    };
    // A time can be estimated when a usable sample lies within half an interval of the same
    // time some days away, and that time is a timestamp. From `t` on, the first such time
    // for one reference day is where the next usable sample at or after that day's time
    // comes into reach. `estimate` gives a value at exactly those times.
    let hope = |t: i64| -> Option<i64> {
        let t = i128::from(t);
        shifts()
            .filter_map(|shift| {
                let from = i64::try_from((t + shift - tolerance).max(*range.start())).ok()?;
                let sample = i128::from(*known.ts.get(known.ts.partition_point(|&u| u < from))?);
                let at = t.max(sample - shift - tolerance).max(range.start() - shift);
                range.contains(&(at + shift)).then_some(at)
            })
            .min()
            .and_then(|at| i64::try_from(at).ok())
    };
    fill(raw, lo, hi, window, Some(grid), estimate, hope)
}
