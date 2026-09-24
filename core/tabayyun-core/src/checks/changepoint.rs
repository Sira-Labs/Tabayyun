//! `tby.changepoint` — abrupt regime changes via PELT with an L2 cost (catalogue #20).
//!
//! The series is first aggregated into time buckets (median per bucket, one day by default)
//! so intra-day seasonality does not read as a sequence of steps and runtime is bounded
//! regardless of sampling rate. The penalty is BIC-like: `beta × sigma² × ln(m)` with
//! `sigma` the robust sigma of bucket-to-bucket differences. A change is reported only when
//! the level jump is both statistically clear (`min_jump_sigma`) and material relative to
//! the series' usual spread (`min_jump_spread`), the new level persists for at least
//! `min_segment_buckets` (one week by default, so weekday/weekend alternation is not a
//! sequence of regime changes) and the change is abrupt: the medians of the buckets just
//! before and just after the change differ by the same margin, which a seasonal ramp cut
//! into a staircase by PELT does not satisfy. Finally the jump must be large against how much
//! the buckets vary anyway inside the two segments it separates (`min_effect_size`): a sunny
//! week after a cloudy one moves the daily median of a solar series by less than the days
//! within either week differ from each other. A series with more surviving changes than
//! `max_findings` shifts level as a matter of course (weather-driven generation) and gets one
//! summary finding instead of a list (ADR-0011).

use super::{metric, Check, CheckContext, CheckOutput};
use crate::error::Result;
use crate::finding::{Dimension, Finding, Severity, Window};
use crate::frame::SeriesFrame;
use crate::profile::{noise_sigma, quantile_f64, Profile};
use crate::time::{format_duration, NS_PER_DAY};
use serde::{Deserialize, Serialize};

pub const ID: &str = "tby.changepoint";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Changepoint {
    /// Aggregation bucket (default one day, which removes daily seasonality).
    pub bucket_ns: i64,
    /// Maximum number of buckets (the bucket is widened if the series is longer).
    pub max_buckets: usize,
    /// Minimum segment length between changepoints, in buckets (persistence of the new
    /// level; also the span of the local before/after comparison).
    pub min_segment_buckets: usize,
    /// Penalty multiplier (higher = fewer changepoints).
    pub beta: f64,
    /// Only report changes whose level jump exceeds this many bucket-noise sigmas.
    pub min_jump_sigma: f64,
    /// ... and this fraction of the series' usual spread (1.4826 × MAD) ...
    pub min_jump_spread: f64,
    /// ... and this many pooled within-segment robust sigmas of the bucket values on either
    /// side of the change (effect size).
    pub min_effect_size: f64,
    /// Fewer non-empty buckets than this → check does not run.
    pub min_buckets: usize,
    /// More reported changes than this collapse into one summary finding for the window.
    pub max_findings: usize,
    /// Changes listed in the summary's evidence (the count is always exact).
    pub max_listed: usize,
    pub severity: Severity,
}

impl Default for Changepoint {
    fn default() -> Self {
        Self {
            bucket_ns: NS_PER_DAY,
            max_buckets: 2000,
            min_segment_buckets: 7,
            beta: 3.0,
            min_jump_sigma: 3.0,
            min_jump_spread: 0.5,
            min_effect_size: 2.0,
            min_buckets: 8,
            max_findings: 20,
            max_listed: 10,
            severity: Severity::Medium,
        }
    }
}

/// PELT for piecewise-constant mean with L2 cost. Returns changepoint indices (start of a
/// new segment) in `0 < cp < n`.
pub fn pelt_l2(x: &[f64], penalty: f64, min_size: usize) -> Vec<usize> {
    let n = x.len();
    if n < 2 * min_size.max(1) {
        return Vec::new();
    }
    let mut cs = vec![0.0; n + 1];
    let mut cs2 = vec![0.0; n + 1];
    for i in 0..n {
        cs[i + 1] = cs[i] + x[i];
        cs2[i + 1] = cs2[i] + x[i] * x[i];
    }
    let cost = |a: usize, b: usize| -> f64 {
        let len = (b - a) as f64;
        let s = cs[b] - cs[a];
        (cs2[b] - cs2[a]) - s * s / len
    };
    let mut f = vec![f64::INFINITY; n + 1];
    f[0] = -penalty;
    let mut last = vec![0usize; n + 1];
    let mut candidates: Vec<usize> = vec![0];
    for t in min_size..=n {
        let mut best = f64::INFINITY;
        let mut arg = 0;
        for &s in &candidates {
            if t - s < min_size {
                continue;
            }
            let v = f[s] + cost(s, t) + penalty;
            if v < best {
                best = v;
                arg = s;
            }
        }
        f[t] = best;
        last[t] = arg;
        // PELT pruning: a start `s` whose cost so far already exceeds the best total can never
        // be optimal for any later end (L2 cost is additive).
        candidates.retain(|&s| t - s < min_size || f[s] + cost(s, t) <= best);
        if t + min_size <= n {
            candidates.push(t);
        }
    }
    let mut cps = Vec::new();
    let mut t = n;
    while t > 0 {
        let s = last[t];
        if s > 0 {
            cps.push(s);
        }
        t = s;
    }
    cps.reverse();
    cps
}

impl Check for Changepoint {
    fn id(&self) -> &'static str {
        ID
    }
    fn dimension(&self) -> Dimension {
        Dimension::Plausibility
    }
    fn default_severity(&self) -> Severity {
        self.severity
    }

    fn run(&self, frame: &SeriesFrame, ctx: &CheckContext) -> Result<CheckOutput> {
        let mut out = CheckOutput::default();
        let (f, _) = frame.normalized();
        let n = f.len();
        if n < 20 {
            return Ok(out);
        }
        // Bucket by time (median per bucket), widening the bucket if the series is very long.
        let (t0, t1) = (f.ts[0], f.ts[n - 1]);
        let span = t1.saturating_sub(t0).max(1);
        let mut bucket_ns = self.bucket_ns.max(1);
        if span / bucket_ns >= self.max_buckets as i64 {
            bucket_ns = (span as f64 / self.max_buckets as f64).ceil() as i64;
        }
        let buckets = (span / bucket_ns + 1) as usize;
        let mut groups: Vec<Vec<f64>> = vec![Vec::new(); buckets];
        for i in 0..n {
            if !f.values[i].is_finite() || !f.quality[i].is_usable() {
                continue;
            }
            let b = ((f.ts[i].saturating_sub(t0) / bucket_ns) as usize).min(buckets - 1);
            groups[b].push(f.values[i]);
        }
        let mut xs: Vec<f64> = Vec::with_capacity(buckets);
        let mut ts: Vec<i64> = Vec::with_capacity(buckets);
        for (b, g) in groups.iter_mut().enumerate() {
            if !g.is_empty() {
                g.sort_by(|a, b| a.partial_cmp(b).unwrap());
                xs.push(quantile_f64(g, 0.5));
                ts.push(t0.saturating_add((b as i64).saturating_mul(bucket_ns)));
            }
        }
        let m = xs.len();
        if m < self.min_buckets {
            return Ok(out);
        }
        let spread = match &ctx.profile {
            Some(p) => p.mad,
            None => Profile::compute(&f).mad,
        }
        .map(|mad| 1.4826 * mad)
        .unwrap_or(0.0);
        let sigma =
            noise_sigma(&ts, &xs, i64::MAX).unwrap_or(0.0).max(f.meta.resolution.unwrap_or(1e-9)).max(1e-9);
        let penalty = self.beta * sigma * sigma * (m as f64).ln();
        let min_size = self.min_segment_buckets.clamp(2, (m / 2).max(2));
        let cps = pelt_l2(&xs, penalty, min_size);
        out.metrics.push(metric(ID, &f, "changepoints", ctx.window.end, cps.len() as f64));
        let mut prev = 0usize;
        let mut bounds: Vec<usize> = cps.clone();
        bounds.push(m);
        let mut seg_means: Vec<f64> = Vec::new();
        for &b in &bounds {
            let seg = &xs[prev..b];
            let mut s = seg.to_vec();
            s.sort_by(|a, b| a.partial_cmp(b).unwrap());
            seg_means.push(quantile_f64(&s, 0.5));
            prev = b;
        }
        let min_jump = (self.min_jump_sigma * sigma).max(self.min_jump_spread * spread);
        let local_median = |lo: usize, hi: usize| {
            let mut s = xs[lo..hi].to_vec();
            s.sort_by(|a, b| a.partial_cmp(b).unwrap());
            quantile_f64(&s, 0.5)
        };
        // Reported changes: (window, jump, local jump, effect size, before, after, cp index).
        let mut changes: Vec<(Window, f64, f64, f64, f64, f64, usize)> = Vec::new();
        for (k, &cp) in cps.iter().enumerate() {
            let jump = seg_means[k + 1] - seg_means[k];
            if jump.abs() < min_jump {
                continue;
            }
            // Abruptness: the buckets immediately around the change must differ as much as
            // the segments do, otherwise this is a ramp that PELT cut into steps.
            let seg_start = bounds.get(k.wrapping_sub(1)).copied().unwrap_or(0);
            let lo = cp.saturating_sub(min_size).max(seg_start);
            let hi = (cp + min_size).min(bounds[k + 1]);
            let local_jump = local_median(cp, hi) - local_median(lo, cp);
            if local_jump.abs() < min_jump || local_jump.signum() != jump.signum() {
                continue;
            }
            // Effect size: pooled robust sigma of the buckets around their segment medians.
            let mut dev: Vec<f64> = xs[seg_start..cp].iter().map(|x| (x - seg_means[k]).abs()).collect();
            dev.extend(xs[cp..bounds[k + 1]].iter().map(|x| (x - seg_means[k + 1]).abs()));
            dev.sort_by(|a, b| a.partial_cmp(b).unwrap());
            let within = (1.4826 * quantile_f64(&dev, 0.5)).max(sigma);
            let effect = jump.abs() / within;
            if effect < self.min_effect_size {
                continue;
            }
            let end = if k + 1 < cps.len() { ts[cps[k + 1]] } else { t1 + 1 };
            changes.push((
                Window::new(ts[cp], end),
                jump,
                local_jump,
                effect,
                seg_means[k],
                seg_means[k + 1],
                cp,
            ));
        }
        out.metrics.push(metric(ID, &f, "level_changes", ctx.window.end, changes.len() as f64));
        let params = serde_json::json!({"sigma": sigma, "min_jump": min_jump, "min_effect_size": self.min_effect_size,
            "min_segment_buckets": min_size, "bucket_ns": bucket_ns, "penalty": penalty});
        let change_json = |c: &(Window, f64, f64, f64, f64, f64, usize)| {
            serde_json::json!({"ts": c.0.start, "jump": c.1, "local_jump": c.2, "effect_size": c.3, "before": c.4, "after": c.5,
                "duration_ns": c.0.duration()})
        };
        if changes.len() > self.max_findings {
            // Level shifts are how this series behaves; one summary keeps the list actionable.
            let span = t1.saturating_sub(t0).max(1);
            let mut largest: Vec<&(Window, f64, f64, f64, f64, f64, usize)> = changes.iter().collect();
            largest.sort_by(|a, b| b.3.total_cmp(&a.3));
            let w = Window::new(changes[0].0.start, t1 + 1);
            out.findings.push(Finding::new(
                ID, &f.meta.id, Dimension::Plausibility, Severity::Low, w,
                ctx.window.overlap_fraction(&w) * 0.5,
                format!(
                    "Level shifts every {} on average: {} abrupt, persistent level changes over {}; shifting level is characteristic of this series and changes are not listed individually",
                    format_duration(span / changes.len() as i64), changes.len(), format_duration(span)
                ),
                {
                    let mut e = serde_json::json!({"summary_of": changes.len(), "max_findings": self.max_findings,
                        "mean_interval_ns": span / changes.len() as i64,
                        "largest_changes": largest.iter().take(self.max_listed).map(|c| change_json(c)).collect::<Vec<_>>()});
                    for (k, v) in params.as_object().unwrap() {
                        e[k] = v.clone();
                    }
                    e
                },
            ));
            return Ok(out);
        }
        for c in &changes {
            let (w, jump, effect) = (c.0, c.1, c.3);
            let mut e = change_json(c);
            for (k, v) in params.as_object().unwrap() {
                e[k] = v.clone();
            }
            out.findings.push(Finding::new(
                ID, &f.meta.id, Dimension::Plausibility, self.severity, w,
                ctx.window.overlap_fraction(&w) * 0.5,
                format!("Level changed by {jump:+.4} ({:.1} noise sigmas, effect size {effect:.1}) and stayed there for {}; confirm as legitimate or mark as a data problem",
                    jump.abs() / sigma, format_duration(w.duration())),
                e,
            ));
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::checks::testutil::*;
    use crate::synth::Rng;

    #[test]
    fn pelt_finds_single_step() {
        let mut x = vec![0.0; 200];
        for v in x.iter_mut().skip(120) {
            *v = 5.0;
        }
        let cps = pelt_l2(&x, 3.0 * (200f64).ln(), 5);
        assert_eq!(cps, vec![120]);
    }

    #[test]
    fn step_in_series_flagged_once() {
        let mut f = base(20 * 1440);
        for v in f.values.iter_mut().skip(12 * 1440) {
            *v += 8.0;
        }
        let out = Changepoint::default().run(&f, &ctx(&f)).unwrap();
        let fs = ids(&out, ID);
        assert_eq!(fs.len(), 1, "{:?}", out.findings);
        assert_eq!(fs[0].evidence["ts"].as_i64().unwrap(), f.ts[12 * 1440]);
    }

    #[test]
    fn sinusoid_without_step_passes() {
        let f = base(20 * 1440);
        let out = Changepoint::default().run(&f, &ctx(&f)).unwrap();
        assert!(ids(&out, ID).is_empty(), "{:?}", out.findings);
    }

    #[test]
    fn weekly_cycle_is_not_a_series_of_regime_changes() {
        // Eight weeks of a daily sinusoid whose level drops on Saturdays and Sundays.
        let mut f = base(56 * 1440);
        for (i, v) in f.values.iter_mut().enumerate() {
            if (i / 1440) % 7 >= 5 {
                *v -= 8.0;
            }
        }
        let out = Changepoint::default().run(&f, &ctx(&f)).unwrap();
        assert!(ids(&out, ID).is_empty(), "{:?}", out.findings);
    }

    #[test]
    fn slow_ramp_is_not_a_changepoint() {
        let mut f = base(60 * 1440);
        for (i, v) in f.values.iter_mut().enumerate() {
            *v += 20.0 * i as f64 / (60.0 * 1440.0); // +20 over two months, one step per minute
        }
        let out = Changepoint::default().run(&f, &ctx(&f)).unwrap();
        assert!(ids(&out, ID).is_empty(), "{:?}", out.findings);
    }

    #[test]
    fn step_inside_weekly_cycle_flagged_once() {
        let mut f = base(56 * 1440);
        for (i, v) in f.values.iter_mut().enumerate() {
            if (i / 1440) % 7 >= 5 {
                *v -= 8.0;
            }
            if i >= 30 * 1440 {
                *v += 25.0;
            }
        }
        let out = Changepoint::default().run(&f, &ctx(&f)).unwrap();
        let fs = ids(&out, ID);
        assert_eq!(fs.len(), 1, "{:?}", out.findings);
        assert_eq!(fs[0].evidence["ts"].as_i64().unwrap(), f.ts[30 * 1440]);
    }

    #[test]
    fn weather_like_regimes_are_not_changepoints() {
        // Ten weeks of daily levels that swing widely within each week and whose weekly
        // averages alternate: the jump between weeks is no larger than the spread inside them.
        let mut f = base(70 * 1440);
        let mut rng = Rng::new(21);
        let mut day_level = 0.0;
        for (i, v) in f.values.iter_mut().enumerate() {
            if i % 1440 == 0 {
                let week = (i / 1440) / 7;
                day_level = if week % 2 == 0 { 20.0 } else { 0.0 } + 25.0 * rng.uniform();
            }
            *v += day_level;
        }
        let out = Changepoint::default().run(&f, &ctx(&f)).unwrap();
        assert!(ids(&out, ID).is_empty(), "{:?}", out.findings);
    }

    #[test]
    fn series_that_shifts_level_all_the_time_gets_one_summary() {
        // A year of daily levels that step to a new random level every ten days.
        let mut f = base(365 * 1440);
        let mut rng = Rng::new(33);
        let mut level = 0.0;
        for (i, v) in f.values.iter_mut().enumerate() {
            if i % (10 * 1440) == 0 {
                level = 40.0 * rng.uniform();
            }
            *v += level;
        }
        let out = Changepoint::default().run(&f, &ctx(&f)).unwrap();
        let fs = ids(&out, ID);
        assert_eq!(fs.len(), 1, "{}", out.findings.len());
        assert_eq!(fs[0].severity, Severity::Low);
        assert!(fs[0].evidence["summary_of"].as_u64().unwrap() > 20);
        assert_eq!(fs[0].evidence["largest_changes"].as_array().unwrap().len(), 10);
        let changes = out.metrics.iter().find(|m| m.name == "level_changes").unwrap().value;
        assert!(changes > 20.0);
    }
}
