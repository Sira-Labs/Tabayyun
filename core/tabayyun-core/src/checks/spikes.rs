//! `tby.spikes` — point outliers via a Hampel filter (catalogue #13).

use super::{expected_interval, metric, Check, CheckContext, CheckOutput};
use crate::error::Result;
use crate::finding::{Dimension, Finding, Severity, Window};
use crate::frame::SeriesFrame;
use crate::profile::{quantile_f64, resolution, Profile};
use serde::{Deserialize, Serialize};

pub const ID: &str = "tby.spikes";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Spikes {
    /// Threshold in robust standard deviations (1.4826 × MAD).
    pub t: f64,
    /// Centred rolling window length in samples (odd).
    pub window: usize,
    /// Floor for the scaled MAD so a locally constant signal does not flag tiny changes;
    /// `None` = series resolution (or the baseline noise floor if larger).
    pub min_scale: Option<f64>,
    pub max_findings: usize,
    pub severity: Severity,
}

impl Default for Spikes {
    fn default() -> Self {
        Self { t: 4.0, window: 21, min_scale: None, max_findings: 500, severity: Severity::Medium }
    }
}

impl Check for Spikes {
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
        let w = self.window.max(3) | 1;
        if n < w {
            return Ok(out);
        }
        let interval = expected_interval(&f, ctx).unwrap_or(1);
        // Scale floor: the series' robust noise sigma or its resolution, whichever is larger.
        // A 21-sample MAD underestimates the scale often enough that, without the floor,
        // ordinary noise is flagged at a few tenths of a percent.
        let floor = self.min_scale.unwrap_or_else(|| {
            let res = frame.meta.resolution.or_else(|| resolution(&f.values)).unwrap_or(0.0);
            let noise = match &ctx.profile {
                Some(p) => p.noise_mad,
                None => Profile::compute(&f).noise_mad,
            }
            .unwrap_or(0.0);
            res.max(noise)
        });
        let half = w / 2;
        let mut buf: Vec<f64> = Vec::with_capacity(w);
        let mut spikes: Vec<(usize, f64, f64, f64)> = Vec::new();
        for i in 0..n {
            let x = f.values[i];
            if !x.is_finite() || !f.quality[i].is_usable() {
                continue;
            }
            let lo = i.saturating_sub(half);
            let hi = (i + half + 1).min(n);
            buf.clear();
            buf.extend(f.values[lo..hi].iter().copied().filter(|v| v.is_finite()));
            if buf.len() < 3 {
                continue;
            }
            buf.sort_by(|a, b| a.partial_cmp(b).unwrap());
            let med = quantile_f64(&buf, 0.5);
            let mut dev: Vec<f64> = buf.iter().map(|v| (v - med).abs()).collect();
            dev.sort_by(|a, b| a.partial_cmp(b).unwrap());
            let scale = (1.4826 * quantile_f64(&dev, 0.5)).max(floor);
            if scale <= 0.0 {
                continue;
            }
            let z = (x - med).abs() / scale;
            if z > self.t {
                spikes.push((i, x, med, z));
            }
        }
        out.metrics.push(metric(ID, &f, "spike_count", ctx.window.end, spikes.len() as f64));
        for (k, (i, x, med, z)) in spikes.iter().enumerate() {
            if k >= self.max_findings {
                break;
            }
            let win = Window::new(f.ts[*i], f.ts[*i] + interval.max(1));
            out.findings.push(Finding::new(
                ID, &f.meta.id, Dimension::Plausibility, self.severity, win,
                1.0 / n as f64,
                format!("Spike: value {x:.4} is {z:.1} robust standard deviations from the local median {med:.4}"),
                serde_json::json!({"ts": f.ts[*i], "value": x, "local_median": med, "z": z, "t": self.t, "window": w}),
            ));
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::checks::testutil::*;
    use crate::synth::{inject, Rng};

    #[test]
    fn finds_injected_spikes_and_little_else() {
        let mut f = base(2880);
        let mut rng = Rng::new(3);
        let idx = inject::spikes(&mut f, &mut rng, 5, 10.0);
        let out = Spikes::default().run(&f, &ctx(&f)).unwrap();
        let found: Vec<i64> = ids(&out, ID).iter().map(|x| x.evidence["ts"].as_i64().unwrap()).collect();
        for i in idx {
            assert!(found.contains(&f.ts[i]), "spike at {i} not found");
        }
        // Gaussian noise at 3 robust sigma yields a small false-positive rate; keep it bounded.
        assert!(found.len() <= 5 + 2880 / 500, "too many spikes: {}", found.len());
    }

    #[test]
    fn constant_signal_with_one_step_is_not_a_spike_storm() {
        let mut f = base(500);
        inject::flatline(&mut f, 0, 500);
        f.values[250] += 0.001; // below the resolution floor once resolution is derived
        let out = Spikes::default().run(&f, &ctx(&f)).unwrap();
        assert!(ids(&out, ID).len() <= 1);
    }
}
