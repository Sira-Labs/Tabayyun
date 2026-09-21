//! `tby.changepoint` — abrupt regime changes via PELT with an L2 cost (catalogue #20).
//!
//! The series is first aggregated into time buckets (median per bucket, one day by default)
//! so intra-day seasonality does not read as a sequence of steps and runtime is bounded
//! regardless of sampling rate. The penalty is BIC-like: `beta × sigma² × ln(m)` with
//! `sigma` the robust sigma of bucket-to-bucket differences. A change is reported only when
//! the level jump is both statistically clear (`min_jump_sigma`) and material relative to
//! the series' usual spread (`min_jump_spread`).

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
    /// Minimum segment length between changepoints, in buckets.
    pub min_segment_buckets: usize,
    /// Penalty multiplier (higher = fewer changepoints).
    pub beta: f64,
    /// Only report changes whose level jump exceeds this many bucket-noise sigmas.
    pub min_jump_sigma: f64,
    /// ... and this fraction of the series' usual spread (1.4826 × MAD).
    pub min_jump_spread: f64,
    /// Fewer non-empty buckets than this → check does not run.
    pub min_buckets: usize,
    pub severity: Severity,
}

impl Default for Changepoint {
    fn default() -> Self {
        Self {
            bucket_ns: NS_PER_DAY,
            max_buckets: 2000,
            min_segment_buckets: 2,
            beta: 3.0,
            min_jump_sigma: 3.0,
            min_jump_spread: 0.5,
            min_buckets: 8,
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
        let span = (t1 - t0).max(1);
        let mut bucket_ns = self.bucket_ns.max(1);
        if span / bucket_ns + 1 > self.max_buckets as i64 {
            bucket_ns = (span as f64 / self.max_buckets as f64).ceil() as i64;
        }
        let buckets = (span / bucket_ns + 1) as usize;
        let mut groups: Vec<Vec<f64>> = vec![Vec::new(); buckets];
        for i in 0..n {
            if !f.values[i].is_finite() || !f.quality[i].is_usable() {
                continue;
            }
            let b = (((f.ts[i] - t0) / bucket_ns) as usize).min(buckets - 1);
            groups[b].push(f.values[i]);
        }
        let mut xs: Vec<f64> = Vec::with_capacity(buckets);
        let mut ts: Vec<i64> = Vec::with_capacity(buckets);
        for (b, g) in groups.iter_mut().enumerate() {
            if !g.is_empty() {
                g.sort_by(|a, b| a.partial_cmp(b).unwrap());
                xs.push(quantile_f64(g, 0.5));
                ts.push(t0 + b as i64 * bucket_ns);
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
        for (k, &cp) in cps.iter().enumerate() {
            let jump = seg_means[k + 1] - seg_means[k];
            if jump.abs() < min_jump {
                continue;
            }
            let end = if k + 1 < cps.len() { ts[cps[k + 1]] } else { t1 + 1 };
            let w = Window::new(ts[cp], end);
            out.findings.push(Finding::new(
                ID, &f.meta.id, Dimension::Plausibility, self.severity, w,
                ctx.window.overlap_fraction(&w) * 0.5,
                format!("Level changed by {jump:+.4} ({:.1} noise sigmas) and stayed there for {}; confirm as legitimate or mark as a data problem",
                    jump.abs() / sigma, format_duration(w.duration())),
                serde_json::json!({"ts": ts[cp], "jump": jump, "before": seg_means[k], "after": seg_means[k + 1],
                    "sigma": sigma, "min_jump": min_jump, "bucket_ns": bucket_ns, "penalty": penalty}),
            ));
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::checks::testutil::*;

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
}
