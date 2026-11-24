//! The operations that work on the window's own samples: mask, clamp, dedupe, the affine
//! transform and the resampling onto a grid.

use super::{invalid, usable, Keep, Patch};
use crate::error::Result;
use crate::finding::Window;
use crate::frame::SeriesFrame;
use crate::quality::Quality;

/// The window's samples as they are, for an operation to change some of.
fn copy(raw: &SeriesFrame, lo: usize, hi: usize) -> Patch {
    Patch {
        ts: raw.ts[lo..hi].to_vec(),
        values: raw.values[lo..hi].to_vec(),
        quality: raw.quality[lo..hi].to_vec(),
        unrepaired: 0,
    }
}

pub(super) fn mask(raw: &SeriesFrame, lo: usize, hi: usize) -> Patch {
    let mut patch = copy(raw, lo, hi);
    patch.values.fill(f64::NAN);
    patch.quality.fill(Quality::Bad);
    patch
}

pub(super) fn clamp(
    raw: &SeriesFrame,
    lo: usize,
    hi: usize,
    min: Option<f64>,
    max: Option<f64>,
) -> Result<Patch> {
    let (physical_min, physical_max) = raw.meta.physical_limits();
    let (min, max) = (min.or(physical_min), max.or(physical_max));
    if min.is_none() && max.is_none() {
        return Err(invalid("clamp needs min or max, or a series with physical limits"));
    }
    if [min, max].iter().flatten().any(|b| !b.is_finite()) {
        return Err(invalid("clamp: min and max must be finite"));
    }
    if matches!((min, max), (Some(a), Some(b)) if a > b) {
        return Err(invalid("clamp: min must not be above max"));
    }
    let mut patch = copy(raw, lo, hi);
    for i in 0..patch.ts.len() {
        // A NaN compares false with both bounds and stays.
        let v = patch.values[i];
        let bound = min.filter(|m| v < *m).or(max.filter(|m| v > *m));
        if let Some(b) = bound {
            patch.values[i] = b;
            patch.quality[i] = Quality::Estimated;
        }
    }
    Ok(patch)
}

pub(super) fn affine(raw: &SeriesFrame, lo: usize, hi: usize, scale: f64, offset: f64) -> Result<Patch> {
    if !scale.is_finite() || !offset.is_finite() || scale == 0.0 {
        return Err(invalid("transform.affine: scale and offset must be finite and scale not zero"));
    }
    let mut patch = copy(raw, lo, hi);
    for v in patch.values.iter_mut().filter(|v| v.is_finite()) {
        *v = scale * *v + offset;
    }
    Ok(patch)
}

/// Mean of the finite values among usable samples, and whether one of them was estimated.
fn usable_mean(raw: &SeriesFrame, range: std::ops::Range<usize>) -> Option<(f64, bool)> {
    let picked: Vec<usize> = range.filter(|&i| usable(raw.values[i], raw.quality[i])).collect();
    let mean = picked.iter().map(|&i| raw.values[i]).sum::<f64>() / picked.len() as f64;
    let estimated = picked.iter().any(|&i| raw.quality[i] == Quality::Estimated);
    // A sum of huge values can overflow; such a mean is no value.
    (!picked.is_empty() && mean.is_finite()).then_some((mean, estimated))
}

pub(super) fn dedupe(raw: &SeriesFrame, lo: usize, hi: usize, keep: Keep) -> Patch {
    let mut patch = Patch::default();
    let mut i = lo;
    while i < hi {
        let end = i + raw.ts[i..hi].iter().take_while(|t| **t == raw.ts[i]).count();
        let first = (raw.values[i], raw.quality[i]);
        let all_same = (i..end).all(|k| {
            raw.quality[k] == first.1
                && (raw.values[k] == first.0 || (raw.values[k].is_nan() && first.0.is_nan()))
        });
        let (value, quality) = match keep {
            _ if all_same => first,
            Keep::First => first,
            Keep::Last => (raw.values[end - 1], raw.quality[end - 1]),
            // Without a usable sample among them there is no mean to take: the first stays.
            Keep::Mean => usable_mean(raw, i..end).map_or(first, |(mean, _)| (mean, Quality::Estimated)),
        };
        patch.push(raw.ts[i], value, quality);
        i = end;
    }
    patch
}

pub(super) fn resample(
    raw: &SeriesFrame,
    lo: usize,
    hi: usize,
    window: Window,
    grid_ns: i64,
) -> Result<Patch> {
    if grid_ns <= 0 {
        return Err(invalid("align.resample: grid_ns must be positive"));
    }
    // Only bins that lie wholly inside the window are rebuilt, so no sample moves out of it.
    let whole = |t: i64| {
        let start = t.div_euclid(grid_ns).checked_mul(grid_ns)?;
        (start >= window.start && start.checked_add(grid_ns)? <= window.end).then_some(start)
    };
    let mut patch = Patch::default();
    let mut i = lo;
    while i < hi {
        let Some(start) = whole(raw.ts[i]) else {
            patch.push(raw.ts[i], raw.values[i], raw.quality[i]);
            i += 1;
            continue;
        };
        let end = i + raw.ts[i..hi].iter().take_while(|t| whole(**t) == Some(start)).count();
        if end == i + 1 && raw.ts[i] == start {
            // One sample, already on the grid: it stays as it is.
            patch.push(start, raw.values[i], raw.quality[i]);
        } else {
            match usable_mean(raw, i..end) {
                Some((mean, estimated)) => {
                    patch.push(start, mean, if estimated { Quality::Estimated } else { Quality::Good })
                }
                None => patch.push(start, f64::NAN, Quality::Bad),
            }
        }
        i = end;
    }
    Ok(patch)
}
