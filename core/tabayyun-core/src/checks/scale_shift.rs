//! `tby.scale_shift` — unit or scale error such as ×1000 or °C↔°F (catalogue #12).

use super::{metric, segments, Check, CheckContext, CheckOutput};
use crate::error::Result;
use crate::finding::{Dimension, Finding, Severity};
use crate::frame::SeriesFrame;
use crate::profile::{quantile_f64, Profile};
use crate::time::NS_PER_DAY;
use serde::{Deserialize, Serialize};

pub const ID: &str = "tby.scale_shift";

const RATIOS: [f64; 6] = [10.0, 100.0, 1000.0, 0.1, 0.01, 0.001];

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct ScaleShift {
    pub segment_ns: i64,
    /// Relative tolerance around a candidate ratio.
    pub ratio_tol: f64,
    pub min_samples: usize,
    pub severity: Severity,
}

impl Default for ScaleShift {
    fn default() -> Self {
        Self { segment_ns: NS_PER_DAY, ratio_tol: 0.10, min_samples: 30, severity: Severity::Critical }
    }
}

impl Check for ScaleShift {
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
        let (profile, source) = match &ctx.profile {
            Some(p) => (p.clone(), "baseline"),
            None => (Profile::compute(&f), "self"),
        };
        let (Some(base_med), Some(base_mad)) = (profile.median, profile.mad) else { return Ok(out) };
        if base_med.abs() < 1e-12 {
            return Ok(out);
        }
        for (s, e, w) in segments(&f, self.segment_ns) {
            let mut seg: Vec<f64> = f.values[s..e].iter().copied().filter(|v| v.is_finite()).collect();
            if seg.len() < self.min_samples {
                continue;
            }
            seg.sort_by(|a, b| a.partial_cmp(b).unwrap());
            let med = quantile_f64(&seg, 0.5);
            let mut dev: Vec<f64> = seg.iter().map(|v| (v - med).abs()).collect();
            dev.sort_by(|a, b| a.partial_cmp(b).unwrap());
            let mad = quantile_f64(&dev, 0.5);
            let ratio = med / base_med;
            out.metrics.push(metric(ID, &f, "median_ratio", w.end, ratio));
            let mut candidate: Option<(String, f64)> = None;
            for r in RATIOS {
                if ((ratio - r) / r).abs() <= self.ratio_tol
                    && (base_mad <= 0.0 || ((mad / base_mad - r) / r).abs() <= 0.5)
                {
                    candidate = Some((format!("×{r}"), r));
                    break;
                }
            }
            if candidate.is_none() && base_mad > 0.0 {
                // °C → °F: median maps by 1.8x + 32, spread by 1.8.
                let f_med = 1.8 * base_med + 32.0;
                if ((med - f_med) / f_med.abs().max(1.0)).abs() <= self.ratio_tol
                    && ((mad / base_mad - 1.8) / 1.8).abs() <= 0.25
                {
                    candidate = Some(("°C→°F".into(), 1.8));
                }
                let c_med = (base_med - 32.0) / 1.8;
                if ((med - c_med) / c_med.abs().max(1.0)).abs() <= self.ratio_tol
                    && ((mad / base_mad - 1.0 / 1.8) * 1.8).abs() <= 0.25
                {
                    candidate = Some(("°F→°C".into(), 1.0 / 1.8));
                }
            }
            if let Some((label, r)) = candidate {
                out.findings.push(Finding::new(
                    ID,
                    &f.meta.id,
                    Dimension::Validity,
                    self.severity,
                    w,
                    ctx.window.overlap_fraction(&w),
                    format!("Values look rescaled by {label}: median {med:.4} vs usual {base_med:.4}"),
                    serde_json::json!({"segment_median": med, "baseline_median": base_med, "ratio": ratio,
                        "candidate": label, "candidate_factor": r, "baseline": source}),
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
    use crate::synth::inject;

    #[test]
    fn thousand_fold_day_flagged() {
        let profile = Profile::compute(&base(3 * 1440));
        let mut f = base(3 * 1440);
        inject::scale(&mut f, 2880, 1440, 1000.0);
        let c = ctx(&f).with_profile(profile);
        let out = ScaleShift::default().run(&f, &c).unwrap();
        let fs = ids(&out, ID);
        assert_eq!(fs.len(), 1, "{:?}", out.findings);
        assert_eq!(fs[0].evidence["candidate"], "×1000");
    }

    #[test]
    fn fahrenheit_day_flagged() {
        let profile = Profile::compute(&base(3 * 1440));
        let mut f = base(3 * 1440);
        for v in f.values.iter_mut().skip(2880) {
            *v = 1.8 * *v + 32.0;
        }
        let c = ctx(&f).with_profile(profile);
        let out = ScaleShift::default().run(&f, &c).unwrap();
        assert_eq!(ids(&out, ID)[0].evidence["candidate"], "°C→°F");
    }

    #[test]
    fn clean_passes() {
        let f = base(3 * 1440);
        assert!(ids(&ScaleShift::default().run(&f, &ctx(&f)).unwrap(), ID).is_empty());
    }
}
