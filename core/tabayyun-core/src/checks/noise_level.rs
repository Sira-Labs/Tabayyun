//! `tby.noise_level` — variance jump or suspicious smoothness per segment (catalogue #15).
//!
//! A segment is reported when its noise ratio is beyond `high`/`low` *and*, once the window
//! holds enough segments, its log ratio is unusual among them (household or process signals
//! legitimately vary several-fold in activity from day to day). When the median segment is
//! itself beyond the thresholds the whole window is noisier or smoother than the baseline
//! and that is one finding. Consecutive segments off in the same direction form one episode
//! finding (ADR-0011).

use super::{
    baseline, episodes, metric, segments, unusual_among, usable_pairs, Check, CheckContext, CheckOutput,
};
use crate::error::Result;
use crate::finding::{Dimension, Finding, Severity, Window};
use crate::frame::SeriesFrame;
use crate::profile::noise_sigma;
use crate::time::format_duration;
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
    /// With at least this many segments, a segment must also be unusual among the window's
    /// segments to be reported on its own.
    pub min_segments_relative: usize,
    /// "Unusual" = log noise ratio more than this many robust sigmas from the segments' median.
    pub unusual_sigmas: f64,
    pub severity: Severity,
}

impl Default for NoiseLevel {
    fn default() -> Self {
        Self {
            segment_ns: NS_PER_DAY,
            high: 2.0,
            low: 0.3,
            min_samples: 50,
            min_segments_relative: 8,
            unusual_sigmas: 3.0,
            severity: Severity::Medium,
        }
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
        // Per segment: index, window, segment sigma, ratio.
        let mut stats: Vec<(usize, Window, f64, f64)> = Vec::new();
        for (k, (s, e, w)) in segments(&f, self.segment_ns).into_iter().enumerate() {
            let (ts, vals) = usable_pairs(&f, s, e);
            if vals.len() < self.min_samples {
                continue;
            }
            let Some(sigma) = noise_sigma(&ts, &vals, gap_cut) else { continue };
            let ratio = sigma / base;
            out.metrics.push(metric(ID, &f, "noise_ratio", w.end, ratio));
            stats.push((k, w, sigma, ratio));
        }
        if stats.is_empty() {
            return Ok(out);
        }
        let logs: Vec<f64> = stats.iter().map(|s| s.3.max(1e-12).ln()).collect();
        let unusual = unusual_among(&logs, self.min_segments_relative, self.unusual_sigmas, true);
        // The typical segment is already off: the whole window is noisier or smoother.
        let mut sorted = logs.clone();
        sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());
        let median_ratio = crate::profile::quantile_f64(&sorted, 0.5).exp();
        if stats.len() >= self.min_segments_relative && (median_ratio > self.high || median_ratio < self.low)
        {
            let noisier = median_ratio > self.high;
            let w = Window::new(stats[0].1.start, stats.last().unwrap().1.end);
            out.findings.push(Finding::new(
                ID, &f.meta.id, Dimension::Plausibility, self.severity, w,
                ctx.window.overlap_fraction(&w),
                if noisier {
                    format!("Noise level is {median_ratio:.1}× the usual throughout ({} segments; baseline {base:.4})", stats.len())
                } else {
                    format!("Signal is suspiciously smooth throughout: noise {median_ratio:.2}× the usual ({} segments; baseline {base:.4})", stats.len())
                },
                serde_json::json!({"whole_window": true, "segments": stats.len(), "median_ratio": median_ratio, "baseline_sigma": base,
                    "direction": if noisier { "noisier" } else { "smoother" }, "baseline": source}),
            ));
        }
        let mut noisier: Vec<(usize, Window, (f64, f64))> = Vec::new();
        let mut smoother: Vec<(usize, Window, (f64, f64))> = Vec::new();
        for (s, &u) in stats.iter().zip(&unusual) {
            if !u {
                continue;
            }
            if s.3 > self.high {
                noisier.push((s.0, s.1, (s.2, s.3)));
            } else if s.3 < self.low {
                smoother.push((s.0, s.1, (s.2, s.3)));
            }
        }
        for (is_noisier, flagged) in [(true, noisier), (false, smoother)] {
            for (w, segs) in episodes(flagged) {
                // The most extreme segment characterises the episode.
                let (sigma, ratio) = segs
                    .iter()
                    .copied()
                    .max_by(|a, b| if is_noisier { a.1.total_cmp(&b.1) } else { b.1.total_cmp(&a.1) })
                    .unwrap();
                let span = if segs.len() == 1 {
                    String::new()
                } else {
                    format!(" for {} consecutive segments ({})", segs.len(), format_duration(w.duration()))
                };
                out.findings.push(Finding::new(
                    ID, &f.meta.id, Dimension::Plausibility, self.severity, w,
                    ctx.window.overlap_fraction(&w),
                    if is_noisier {
                        format!("Noise level up to {ratio:.1}× the usual{span} ({sigma:.4} vs {base:.4})")
                    } else {
                        format!("Signal is suspiciously smooth{span}: noise down to {ratio:.2}× the usual ({sigma:.4} vs {base:.4})")
                    },
                    serde_json::json!({"whole_window": false, "segments": segs.len(), "segment_sigma": sigma, "baseline_sigma": base, "ratio": ratio,
                        "ratio_per_segment": segs.iter().map(|s| s.1).collect::<Vec<_>>(),
                        "direction": if is_noisier { "noisier" } else { "smoother" }, "baseline": source}),
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

    #[test]
    fn consecutive_noisy_days_are_one_episode() {
        let profile = crate::Profile::compute(&base(10 * 1440));
        let mut f = base(10 * 1440);
        let mut rng = Rng::new(11);
        for v in f.values.iter_mut().skip(2 * 1440).take(3 * 1440) {
            *v += 2.0 * rng.normal(); // days 3..5 noisier
        }
        let c = ctx(&f).with_profile(profile);
        let out = NoiseLevel::default().run(&f, &c).unwrap();
        let fs = ids(&out, ID);
        assert_eq!(fs.len(), 1, "{:?}", out.findings);
        assert_eq!(fs[0].evidence["segments"], 3);
        assert_eq!(fs[0].window.start, f.ts[2 * 1440]);
        assert!(fs[0].summary.contains("3 consecutive segments"));
    }

    #[test]
    fn whole_window_noisier_than_external_baseline_is_one_finding() {
        let profile = crate::Profile::compute(&base(10 * 1440));
        let mut f = base(10 * 1440);
        let mut rng = Rng::new(5);
        for v in f.values.iter_mut() {
            *v += 2.0 * rng.normal();
        }
        let c = ctx(&f).with_profile(profile);
        let out = NoiseLevel::default().run(&f, &c).unwrap();
        let fs = ids(&out, ID);
        assert_eq!(fs.len(), 1, "{:?}", out.findings);
        assert_eq!(fs[0].evidence["whole_window"], true);
        assert_eq!(fs[0].evidence["direction"], "noisier");
    }

    #[test]
    fn day_to_day_activity_spread_is_not_listed_day_by_day() {
        // Thirty days whose noise alternates 0.5× … 3× around the baseline: with a self
        // baseline no day stands out among the days.
        let mut f = base(30 * 1440);
        let mut rng = Rng::new(9);
        for (i, v) in f.values.iter_mut().enumerate() {
            let day = i / 1440;
            let scale = [0.2, 0.5, 1.0, 1.5, 2.0, 3.0][day % 6];
            *v += scale * rng.normal();
        }
        let out = NoiseLevel::default().run(&f, &ctx(&f)).unwrap();
        assert!(ids(&out, ID).len() <= 2, "{:?}", out.findings);
    }
}
