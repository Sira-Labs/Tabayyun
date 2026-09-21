//! `tby.distribution_drift` — PSI and normalised Wasserstein distance per segment against the
//! baseline quantile grid (catalogue #19).
//!
//! A segment is reported when it exceeds the absolute thresholds *and*, once the window holds
//! enough segments, its drift is unusual among them: a series with strong daily or seasonal
//! cycles differs from its pooled baseline on most days, and listing every day is noise.
//! When the median segment itself exceeds the alert level the whole window drifted and that
//! is one finding. Consecutive drifted segments form one episode finding (ADR-0011).

use super::{
    baseline, episodes, metric, segments, unusual_among, usable_values, Check, CheckContext, CheckOutput,
};
use crate::error::Result;
use crate::finding::{Dimension, Finding, Severity, Window};
use crate::frame::SeriesFrame;
use crate::profile::quantile_f64;
use crate::time::format_duration;
use crate::time::NS_PER_DAY;
use serde::{Deserialize, Serialize};

pub const ID: &str = "tby.distribution_drift";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct DistributionDrift {
    pub segment_ns: i64,
    /// Population stability index thresholds (10 baseline-decile bins).
    pub psi_warn: f64,
    pub psi_alert: f64,
    /// Wasserstein-1 distance normalised by the baseline inter-quartile range.
    pub wasserstein_alert: f64,
    pub min_samples: usize,
    /// With at least this many segments, a segment must also be unusual among the window's
    /// segments to be reported on its own.
    pub min_segments_relative: usize,
    /// "Unusual" = drift score more than this many robust sigmas above the segments' median.
    pub unusual_sigmas: f64,
    pub severity: Severity,
}

impl Default for DistributionDrift {
    fn default() -> Self {
        Self {
            segment_ns: NS_PER_DAY,
            psi_warn: 0.10,
            psi_alert: 0.25,
            wasserstein_alert: 0.10,
            min_samples: 100,
            min_segments_relative: 8,
            unusual_sigmas: 3.0,
            severity: Severity::Medium,
        }
    }
}

/// PSI of `sorted` against decile edges `edges` (9 interior edges from the baseline).
fn psi(sorted: &[f64], edges: &[f64]) -> f64 {
    let n = sorted.len() as f64;
    let mut psi = 0.0;
    let mut lo = 0usize;
    for b in 0..=edges.len() {
        let hi = if b < edges.len() { sorted.partition_point(|v| *v <= edges[b]) } else { sorted.len() };
        let actual = ((hi - lo) as f64 / n).max(1e-4);
        let expected = 1.0 / (edges.len() + 1) as f64;
        psi += (actual - expected) * (actual / expected).ln();
        lo = hi;
    }
    psi
}

fn median_of(values: impl Iterator<Item = f64>) -> f64 {
    let mut v: Vec<f64> = values.collect();
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    quantile_f64(&v, 0.5)
}

/// Copy every field of `extra` into `target` (both JSON objects).
fn merge(target: &mut serde_json::Value, extra: &serde_json::Value) {
    if let (Some(t), Some(e)) = (target.as_object_mut(), extra.as_object()) {
        for (k, v) in e {
            t.insert(k.clone(), v.clone());
        }
    }
}

impl Check for DistributionDrift {
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
        if profile.quantiles.len() != 21 {
            return Ok(out);
        }
        let q = &profile.quantiles;
        let iqr = (q[15] - q[5]).max(profile.resolution.unwrap_or(0.0)).max(1e-12);
        let edges: Vec<f64> = (1..10).map(|k| q[2 * k]).collect(); // deciles 10..90 %
                                                                   // PSI assumes 10 % of the baseline in every bin; with tied deciles (constant or heavily
                                                                   // quantised baseline) that assumption is false, so PSI is skipped and only the
                                                                   // Wasserstein distance is used for such series.
        let psi_valid = edges.windows(2).all(|w| w[1] > w[0]);
        // Per segment: index, window, psi, wasserstein, drift score in alert units.
        let mut stats: Vec<(usize, Window, f64, f64, f64)> = Vec::new();
        for (k, (s, e, w)) in segments(&f, self.segment_ns).into_iter().enumerate() {
            let mut seg = usable_values(&f, s, e);
            if seg.len() < self.min_samples {
                continue;
            }
            seg.sort_by(|a, b| a.partial_cmp(b).unwrap());
            let psi_v = if psi_valid { psi(&seg, &edges) } else { 0.0 };
            let wass = (0..=20).map(|k| (quantile_f64(&seg, k as f64 / 20.0) - q[k]).abs()).sum::<f64>()
                / 21.0
                / iqr;
            out.metrics.push(metric(ID, &f, "psi", w.end, psi_v));
            out.metrics.push(metric(ID, &f, "wasserstein_norm", w.end, wass));
            let score = (psi_v / self.psi_alert).max(wass / self.wasserstein_alert);
            stats.push((k, w, psi_v, wass, score));
        }
        if stats.is_empty() {
            return Ok(out);
        }
        let scores: Vec<f64> = stats.iter().map(|s| s.4).collect();
        let unusual = unusual_among(&scores, self.min_segments_relative, self.unusual_sigmas, false);
        let params = serde_json::json!({"psi_warn": self.psi_warn, "psi_alert": self.psi_alert,
            "wasserstein_alert": self.wasserstein_alert, "segment_ns": self.segment_ns, "baseline": source});
        // The typical segment already exceeds the alert level: the whole window drifted.
        let mut sorted = scores.clone();
        sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());
        let median_score = quantile_f64(&sorted, 0.5);
        if stats.len() >= self.min_segments_relative && median_score >= 1.0 {
            let w = Window::new(stats[0].1.start, stats.last().unwrap().1.end);
            let psi_median = median_of(stats.iter().map(|s| s.2));
            let wass_median = median_of(stats.iter().map(|s| s.3));
            let mut evidence = serde_json::json!({"whole_window": true, "segments": stats.len(),
                "median_drift_score": median_score, "psi_median": psi_median, "wasserstein_median": wass_median,
                "psi_valid": psi_valid});
            merge(&mut evidence, &params);
            out.findings.push(Finding::new(
                ID, &f.meta.id, Dimension::Plausibility, self.severity, w,
                ctx.window.overlap_fraction(&w),
                format!("Value distribution differs from the baseline throughout: the median of {} segments is {median_score:.1}× the alert level (PSI {psi_median:.3}, normalised Wasserstein {wass_median:.3}); re-baseline or accept",
                    stats.len()),
                evidence,
            ));
        }
        let flagged: Vec<(usize, Window, (f64, f64, bool))> = stats
            .iter()
            .zip(&unusual)
            .filter(|(s, &u)| u && (s.4 >= 1.0 || s.2 >= self.psi_warn))
            .map(|(s, _)| (s.0, s.1, (s.2, s.3, s.4 >= 1.0)))
            .collect();
        for (w, segs) in episodes(flagged) {
            let alert = segs.iter().any(|s| s.2);
            let psi_max = segs.iter().map(|s| s.0).fold(f64::MIN, f64::max);
            let wass_max = segs.iter().map(|s| s.1).fold(f64::MIN, f64::max);
            let summary = if segs.len() == 1 {
                format!("Value distribution shifted: PSI {psi_max:.3} (warn {:.2}, alert {:.2}), normalised Wasserstein {wass_max:.3}",
                    self.psi_warn, self.psi_alert)
            } else {
                format!("Value distribution shifted for {} consecutive segments ({}): PSI up to {psi_max:.3} (warn {:.2}, alert {:.2}), normalised Wasserstein up to {wass_max:.3}",
                    segs.len(), format_duration(w.duration()), self.psi_warn, self.psi_alert)
            };
            let mut evidence = serde_json::json!({"whole_window": false, "segments": segs.len(), "psi": psi_max, "psi_valid": psi_valid, "wasserstein_norm": wass_max,
                "psi_per_segment": segs.iter().map(|s| s.0).collect::<Vec<_>>(),
                "wasserstein_per_segment": segs.iter().map(|s| s.1).collect::<Vec<_>>()});
            merge(&mut evidence, &params);
            out.findings.push(Finding::new(
                ID,
                &f.meta.id,
                Dimension::Plausibility,
                if alert { self.severity } else { Severity::Low },
                w,
                ctx.window.overlap_fraction(&w),
                summary,
                evidence,
            ));
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::checks::testutil::*;
    use crate::Profile;

    #[test]
    fn shifted_day_flagged() {
        let profile = Profile::compute(&base(3 * 1440));
        let mut f = base(3 * 1440);
        for v in f.values.iter_mut().skip(2880) {
            *v += 6.0; // shift by ~0.5 amplitude on day 3
        }
        let c = ctx(&f).with_profile(profile);
        let out = DistributionDrift::default().run(&f, &c).unwrap();
        let fs = ids(&out, ID);
        assert_eq!(fs.len(), 1, "{:?}", out.findings);
        assert_eq!(fs[0].severity, Severity::Medium);
    }

    #[test]
    fn constant_baseline_does_not_alert_on_unchanged_constant() {
        let mut f = base(3 * 1440);
        for v in f.values.iter_mut() {
            *v = 42.0;
        }
        let profile = Profile::compute(&f);
        let c = ctx(&f).with_profile(profile);
        let out = DistributionDrift::default().run(&f, &c).unwrap();
        assert!(ids(&out, ID).is_empty(), "{:?}", out.findings);
    }

    #[test]
    fn same_distribution_passes() {
        let profile = Profile::compute(&base(3 * 1440));
        let f = base(3 * 1440);
        let c = ctx(&f).with_profile(profile);
        let out = DistributionDrift::default().run(&f, &c).unwrap();
        // Day-to-day sinusoid phases are identical, so daily distributions match the 3-day one.
        assert!(ids(&out, ID).iter().all(|x| x.severity == Severity::Low), "{:?}", out.findings);
    }

    #[test]
    fn consecutive_shifted_days_are_one_episode() {
        let profile = Profile::compute(&base(10 * 1440));
        let mut f = base(10 * 1440);
        for v in f.values.iter_mut().skip(3 * 1440).take(4 * 1440) {
            *v += 6.0; // days 4..7 shifted
        }
        let c = ctx(&f).with_profile(profile);
        let out = DistributionDrift::default().run(&f, &c).unwrap();
        let fs = ids(&out, ID);
        assert_eq!(fs.len(), 1, "{:?}", out.findings);
        assert_eq!(fs[0].evidence["segments"], 4);
        assert_eq!(fs[0].window.start, f.ts[3 * 1440]);
        assert_eq!(fs[0].window.end, f.ts[7 * 1440 - 1] + 1);
        assert_eq!(fs[0].severity, Severity::Medium);
        assert!(fs[0].summary.contains("4 consecutive segments"));
    }

    #[test]
    fn separated_shifted_days_stay_separate() {
        let profile = Profile::compute(&base(10 * 1440));
        let mut f = base(10 * 1440);
        for v in f.values.iter_mut().skip(2 * 1440).take(1440) {
            *v += 6.0;
        }
        for v in f.values.iter_mut().skip(6 * 1440).take(1440) {
            *v += 6.0;
        }
        let c = ctx(&f).with_profile(profile);
        let out = DistributionDrift::default().run(&f, &c).unwrap();
        assert_eq!(ids(&out, ID).len(), 2, "{:?}", out.findings);
    }

    #[test]
    fn whole_window_shift_against_external_baseline_is_one_finding() {
        let profile = Profile::compute(&base(10 * 1440));
        let mut f = base(10 * 1440);
        for v in f.values.iter_mut() {
            *v += 6.0; // every day shifted relative to the reference period
        }
        let c = ctx(&f).with_profile(profile);
        let out = DistributionDrift::default().run(&f, &c).unwrap();
        let fs = ids(&out, ID);
        assert_eq!(fs.len(), 1, "{:?}", out.findings);
        assert_eq!(fs[0].evidence["whole_window"], true);
        assert_eq!(fs[0].severity, Severity::Medium);
        assert_eq!(fs[0].window.start, f.ts[0]);
    }

    #[test]
    fn days_that_all_differ_from_the_pooled_baseline_are_not_listed_one_by_one() {
        // A slow ramp: with a self baseline every day differs from the pooled distribution,
        // but no day is unusual among the days.
        let mut f = base(30 * 1440);
        for (i, v) in f.values.iter_mut().enumerate() {
            *v += 60.0 * i as f64 / (30.0 * 1440.0);
        }
        let out = DistributionDrift::default().run(&f, &ctx(&f)).unwrap();
        let fs = ids(&out, ID);
        // One whole-window finding, plus at most the two ends of the ramp as episodes.
        assert!(fs.len() <= 3, "{:?}", out.findings);
        assert_eq!(fs.iter().filter(|x| x.evidence["whole_window"] == true).count(), 1, "{:?}", out.findings);
    }
}
