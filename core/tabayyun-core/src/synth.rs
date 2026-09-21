//! Deterministic synthetic series with injected faults.
//!
//! The generator is a tiny xorshift64* PRNG so that Rust and Python fixtures can be made
//! identical without depending on a random-number crate's version.

use crate::frame::{SeriesFrame, SeriesMeta};
use crate::quality::Quality;
use crate::time::NS_PER_SEC;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone)]
pub struct Rng(u64);

impl Rng {
    pub fn new(seed: u64) -> Self {
        Self(seed.max(1))
    }

    pub fn next_u64(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }

    /// Uniform in [0, 1).
    pub fn uniform(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 / (1u64 << 53) as f64
    }

    /// Standard normal via Box–Muller.
    pub fn normal(&mut self) -> f64 {
        let u1 = self.uniform().max(1e-12);
        let u2 = self.uniform();
        (-2.0 * u1.ln()).sqrt() * (2.0 * std::f64::consts::PI * u2).cos()
    }

    pub fn below(&mut self, n: usize) -> usize {
        (self.uniform() * n as f64) as usize
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SynthSpec {
    pub series_id: String,
    pub start_ns: i64,
    pub n: usize,
    pub interval_ns: i64,
    pub base: f64,
    pub amplitude: f64,
    /// Period of the sinusoid in samples.
    pub period: f64,
    pub noise_sd: f64,
    pub seed: u64,
}

impl Default for SynthSpec {
    fn default() -> Self {
        Self {
            series_id: "synthetic".into(),
            start_ns: 1_700_000_000 * NS_PER_SEC,
            n: 24 * 60,
            interval_ns: 60 * NS_PER_SEC,
            base: 50.0,
            amplitude: 10.0,
            period: 24.0 * 60.0,
            noise_sd: 0.5,
            seed: 42,
        }
    }
}

/// Clean base signal: sinusoid + Gaussian noise, regular sampling, all Good quality.
pub fn generate(spec: &SynthSpec) -> SeriesFrame {
    let mut rng = Rng::new(spec.seed);
    let ts: Vec<i64> = (0..spec.n as i64).map(|i| spec.start_ns + i * spec.interval_ns).collect();
    let values: Vec<f64> = (0..spec.n)
        .map(|i| {
            spec.base
                + spec.amplitude * (2.0 * std::f64::consts::PI * i as f64 / spec.period).sin()
                + spec.noise_sd * rng.normal()
        })
        .collect();
    let meta =
        SeriesMeta { expected_interval_ns: Some(spec.interval_ns), ..SeriesMeta::new(&spec.series_id) };
    SeriesFrame::with_default_quality(meta, ts, values).expect("generated lengths match")
}

/// Fault injectors. Each mutates the frame in place and returns the affected index range or
/// indices so tests can assert on detection.
pub mod inject {
    use super::*;

    /// Remove samples `[from, from+len)` (a gap).
    pub fn gap(f: &mut SeriesFrame, from: usize, len: usize) -> (i64, i64) {
        let to = (from + len).min(f.len());
        let start = f.ts[from];
        let end = f.ts[to.min(f.len() - 1)];
        f.ts.drain(from..to);
        f.values.drain(from..to);
        f.quality.drain(from..to);
        (start, end)
    }

    /// Hold the value constant over `[from, from+len)`.
    pub fn flatline(f: &mut SeriesFrame, from: usize, len: usize) -> (usize, usize) {
        let to = (from + len).min(f.len());
        let v = f.values[from];
        for x in &mut f.values[from..to] {
            *x = v;
        }
        (from, to)
    }

    /// Add spikes of `magnitude` at `count` pseudo-random positions.
    pub fn spikes(f: &mut SeriesFrame, rng: &mut Rng, count: usize, magnitude: f64) -> Vec<usize> {
        let mut idx = Vec::with_capacity(count);
        for _ in 0..count {
            let i = rng.below(f.len());
            f.values[i] += if rng.uniform() < 0.5 { magnitude } else { -magnitude };
            idx.push(i);
        }
        idx
    }

    /// Replace values in `[from, from+len)` with NaN.
    pub fn nans(f: &mut SeriesFrame, from: usize, len: usize) -> (usize, usize) {
        let to = (from + len).min(f.len());
        for x in &mut f.values[from..to] {
            *x = f64::NAN;
        }
        (from, to)
    }

    /// Set values in `[from, from+len)` to `value` (e.g. out of physical range or negative).
    pub fn set(f: &mut SeriesFrame, from: usize, len: usize, value: f64) -> (usize, usize) {
        let to = (from + len).min(f.len());
        for x in &mut f.values[from..to] {
            *x = value;
        }
        (from, to)
    }

    /// Mark quality over `[from, from+len)`.
    pub fn quality(f: &mut SeriesFrame, from: usize, len: usize, q: Quality) -> (usize, usize) {
        let to = (from + len).min(f.len());
        for x in &mut f.quality[from..to] {
            *x = q;
        }
        (from, to)
    }

    /// Duplicate sample `i` (exact copy) `times` times, appended at the end (unsorted).
    pub fn duplicate(f: &mut SeriesFrame, i: usize, times: usize) {
        for _ in 0..times {
            f.ts.push(f.ts[i]);
            f.values.push(f.values[i]);
            f.quality.push(f.quality[i]);
        }
    }

    /// Duplicate timestamp `i` with a different value (conflict).
    pub fn conflicting_duplicate(f: &mut SeriesFrame, i: usize, delta: f64) {
        f.ts.push(f.ts[i]);
        f.values.push(f.values[i] + delta);
        f.quality.push(f.quality[i]);
    }

    /// Swap samples `i` and `j` so timestamps arrive out of order.
    pub fn swap(f: &mut SeriesFrame, i: usize, j: usize) {
        f.ts.swap(i, j);
        f.values.swap(i, j);
        f.quality.swap(i, j);
    }

    /// Multiply values in `[from, from+len)` by `factor` (unit/scale error).
    pub fn scale(f: &mut SeriesFrame, from: usize, len: usize, factor: f64) -> (usize, usize) {
        let to = (from + len).min(f.len());
        for x in &mut f.values[from..to] {
            *x *= factor;
        }
        (from, to)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deterministic() {
        let a = generate(&SynthSpec::default());
        let b = generate(&SynthSpec::default());
        assert_eq!(a, b);
        assert_eq!(a.len(), 1440);
    }
}
