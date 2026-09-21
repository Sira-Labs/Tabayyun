//! `tby.noise_level` — variance jump or suspicious smoothness per segment (catalogue #15).

use super::{baseline, metric, segments, usable_pairs, Check, CheckContext, CheckOutput};
use crate::error::Result;
use crate::finding::{Dimension, Finding, Severity};
use crate::frame::SeriesFrame;
use crate::profile::noise_sigma;
use crate::time::NS_PER_DAY;
use serde::{Deserialize, Serialize};

pub const ID: &str = "tby.noise_level";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct NoiseLevel {
    pub segment_ns: i64,
    /// Segment noise / baseline noise above this = noisier.
    pub high: f64,
    /// Below this = suspiciously smooth (filtered, interpolated, compression changed).
    pub low: f64,
    pub min_samples: usize,
    pub severity: Severity,
}

impl Default for NoiseLevel {
    fn default() -> Self {
        Self { segment_ns: NS_PER_DAY, high: 2.0, low: 0.3, min_samples: 50, severity: Severity::Medium }
    }
}

impl Check for NoiseLevel {
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
        let (profile, source) = baseline(ctx, &f);
        let Some(base) = profile.noise_mad.filter(|s| *s > 0.0) else { return Ok(out) };
        let gap_cut = profile.expected_interval_ns.map(|i| 3 * i).unwrap_or(i64::MAX);
        for (s, e, w) in segments(&f, self.segment_ns) {
            let (ts, vals) = usable_pairs(&f, s, e);
            if vals.len() < self.min_samples {
                continue;
            }
            let Some(sigma) = noise_sigma(&ts, &vals, gap_cut) else { continue };
            let ratio = sigma / base;
            out.metrics.push(metric(ID, &f, "noise_ratio", w.end, ratio));
            if ratio > self.high || ratio < self.low {
                let noisier = ratio > self.high;
                out.findings.push(Finding::new(
                    ID, &f.meta.id, Dimension::Plausibility, self.severity, w,
                    ctx.window.overlap_fraction(&w),
                    if noisier {
                        format!("Noise level {ratio:.1}× the usual ({sigma:.4} vs {base:.4})")
                    } else {
                        format!("Signal is suspiciously smooth: noise {ratio:.2}× the usual ({sigma:.4} vs {base:.4})")
                    },
                    serde_json::json!({"segment_sigma": sigma, "baseline_sigma": base, "ratio": ratio,
                        "direction": if noisier { "noisier" } else { "smoother" }, "baseline": source}),
                ));
            }
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
    fn mostly_null_day_is_not_judged() {
        let profile = crate::Profile::compute(&base(3 * 1440));
        let mut f = base(3 * 1440);
        for v in f.values.iter_mut().skip(2880).take(1438) {
            *v = f64::NAN;
        }
        let c = ctx(&f).with_profile(profile);
        assert!(ids(&NoiseLevel::default().run(&f, &c).unwrap(), ID).is_empty());
    }

    #[test]
    fn noisy_day_and_smooth_day() {
        let profile = crate::Profile::compute(&base(4 * 1440));
        let mut f = base(4 * 1440);
        let mut rng = Rng::new(11);
        for v in f.values.iter_mut().skip(2880).take(1440) {
            *v += 2.0 * rng.normal(); // day 3: 4× noisier
        }
        let (a, b) = (f.values[4320], f.values[5759]);
        for (k, v) in f.values.iter_mut().enumerate().skip(4320) {
            *v = a + (b - a) * (k - 4320) as f64 / 1439.0; // day 4: perfectly smooth
        }
        let c = ctx(&f).with_profile(profile);
        let out = NoiseLevel::default().run(&f, &c).unwrap();
        let fs = ids(&out, ID);
        assert_eq!(fs.len(), 2, "{:?}", out.findings);
        assert_eq!(fs[0].evidence["direction"], "noisier");
        assert_eq!(fs[1].evidence["direction"], "smoother");
    }
}
