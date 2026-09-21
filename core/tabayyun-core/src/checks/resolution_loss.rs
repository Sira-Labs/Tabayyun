//! `tby.resolution_loss` — quantization / precision drop per segment (catalogue #16).

use super::{metric, Check, CheckContext, CheckOutput};
use crate::error::Result;
use crate::finding::{Dimension, Finding, Severity, Window};
use crate::frame::SeriesFrame;
use crate::profile::{resolution, Profile};
use crate::time::NS_PER_DAY;
use serde::{Deserialize, Serialize};

pub const ID: &str = "tby.resolution_loss";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct ResolutionLoss {
    /// Segment length in ns over which distinct values and step size are measured.
    pub segment_ns: i64,
    /// Finding if a segment's distinct-value count per sample drops below this ratio of the baseline's.
    pub min_distinct_ratio: f64,
    /// Finding if a segment's smallest step is more than this factor above the baseline's.
    pub step_factor: f64,
    /// Segments with fewer samples than this are ignored.
    pub min_samples: usize,
    pub severity: Severity,
}

impl Default for ResolutionLoss {
    fn default() -> Self {
        Self {
            segment_ns: NS_PER_DAY,
            min_distinct_ratio: 0.2,
            step_factor: 5.0,
            min_samples: 50,
            severity: Severity::Medium,
        }
    }
}

impl Check for ResolutionLoss {
    fn id(&self) -> &'static str {
        ID
    }
    fn dimension(&self) -> Dimension {
        Dimension::Validity
    }
    fn default_severity(&self) -> Severity {
        self.severity
    }

    fn run(&self, frame: &SeriesFrame, ctx: &CheckContext) -> Result<CheckOutput> {
        let mut out = CheckOutput::default();
        let (f, _) = frame.normalized();
        let n = f.len();
        if n < self.min_samples {
            return Ok(out);
        }
        let (profile, source) = match &ctx.profile {
            Some(p) => (p.clone(), "baseline"),
            None => (Profile::compute(&f), "self"),
        };
        let base_density =
            if profile.n_finite > 0 { profile.distinct_values as f64 / profile.n_finite as f64 } else { 0.0 };
        let base_step = profile.resolution.unwrap_or(0.0);
        if base_density <= 0.0 {
            return Ok(out);
        }
        let start = f.ts[0];
        let mut s = 0usize;
        while s < n {
            let seg_end_ts = start + ((f.ts[s] - start) / self.segment_ns + 1) * self.segment_ns;
            let mut e = s;
            while e < n && f.ts[e] < seg_end_ts {
                e += 1;
            }
            let seg: Vec<f64> = f.values[s..e].iter().copied().filter(|v| v.is_finite()).collect();
            if seg.len() >= self.min_samples {
                let mut sorted = seg.clone();
                sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());
                let distinct = 1 + sorted.windows(2).filter(|w| w[0] != w[1]).count();
                let density = distinct as f64 / seg.len() as f64;
                let step = resolution(&f.values[s..e]).unwrap_or(0.0);
                let w = Window::new(f.ts[s], f.ts[e - 1] + 1);
                let density_drop = density < self.min_distinct_ratio * base_density;
                // A larger step only counts when the segment actually sits on a grid of that
                // step; the raw minimum step of a noisy continuous signal grows with fewer
                // samples and is not evidence of quantization.
                let step_growth =
                    base_step > 0.0 && step > self.step_factor * base_step && on_grid(&seg, step);
                if density_drop || step_growth {
                    out.findings.push(Finding::new(
                        ID, &f.meta.id, Dimension::Validity, self.severity, w,
                        ctx.window.overlap_fraction(&w),
                        if density_drop {
                            format!("Only {distinct} distinct values in {} samples ({:.0}% of the usual variety)", seg.len(), 100.0 * density / base_density)
                        } else {
                            format!("Smallest step grew from {base_step:.6} to {step:.6} (resolution lost)")
                        },
                        serde_json::json!({"distinct": distinct, "samples": seg.len(), "density": density, "baseline_density": base_density,
                            "step": step, "baseline_step": base_step, "baseline": source}),
                    ));
                }
                out.metrics.push(metric(ID, &f, "distinct_density", w.end, density));
            }
            s = e.max(s + 1);
        }
        Ok(out)
    }
}

/// True when at least 99 % of `values` are integer multiples of `step` (within 1e-6·step).
fn on_grid(values: &[f64], step: f64) -> bool {
    if step <= 0.0 || values.is_empty() {
        return false;
    }
    let tol = step * 1e-6;
    let hits = values.iter().filter(|v| ((*v / step).round() * step - *v).abs() <= tol).count();
    hits as f64 / values.len() as f64 >= 0.99
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::checks::testutil::*;

    #[test]
    fn rounded_day_is_flagged() {
        let profile = Profile::compute(&base(3 * 1440));
        let mut f = base(3 * 1440);
        for v in f.values.iter_mut().skip(2880) {
            *v = (*v / 5.0).round() * 5.0; // last day quantised to 5 units
        }
        let c = ctx(&f).with_profile(profile);
        let out = ResolutionLoss::default().run(&f, &c).unwrap();
        let fs = ids(&out, ID);
        assert_eq!(fs.len(), 1, "{:?}", out.findings);
    }

    #[test]
    fn clean_passes() {
        let f = base(3 * 1440);
        assert!(ids(&ResolutionLoss::default().run(&f, &ctx(&f)).unwrap(), ID).is_empty());
    }
}
