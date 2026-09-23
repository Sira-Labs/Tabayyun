//! `tby.seasonality_break` — a periodic pattern is lost or changes period (catalogue #21,
//! spec 012).
//!
//! The series is regularised (`seasonal::regularise`) and cut into epoch-anchored segments of
//! `segment` (auto: max(4 × period, 7 d)); a segment counts when at least half of its bins
//! hold a value. The reference period is the shortest candidate that at least half of the
//! first `ref_segments` segments detect, and the reference strength is their median strength
//! at that period. Only later segments are judged, so a segment never takes part in its own
//! reference (the run's profile is computed from the same data and is not used; spec 012,
//! edits). A judged segment is broken when another period appears with strength ≥
//! `min_strength` (`period_changed`) or its strength at the reference period falls below
//! (1 − `drop`) × reference (`weaker`); consecutive broken segments form one finding.

use super::{duration_param, episodes, expected_interval, metric, Check, CheckContext, CheckOutput};
use crate::error::{Error, Result};
use crate::finding::{Dimension, Finding, Severity, Window};
use crate::frame::SeriesFrame;
use crate::profile::median_mad;
use crate::seasonal::{detect_values, regularise, seasonal_strength};
use crate::time::{format_duration, NS_PER_DAY, NS_PER_HOUR};
use serde::{Deserialize, Serialize};

pub const ID: &str = "tby.seasonality_break";
/// A segment is judged only when at least this share of its bins hold a value.
const MIN_COVERAGE: f64 = 0.5;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct SeasonalityBreak {
    /// A reference weaker than this is not seasonal; the check only reports metrics.
    pub min_strength: f64,
    /// Flag when a segment's strength < (1 − drop) × reference.
    pub drop: f64,
    /// Candidate periods.
    pub candidates: Vec<String>,
    /// Segment length: `auto` (max(4 × period, 7 d)) or a duration.
    pub segment: String,
    /// Leading segments that form the reference.
    pub ref_segments: usize,
    pub severity: Severity,
}

impl Default for SeasonalityBreak {
    fn default() -> Self {
        Self {
            min_strength: 0.6,
            drop: 0.5,
            candidates: vec!["1d".into(), "7d".into(), "365d".into()],
            segment: "auto".into(),
            ref_segments: 4,
            severity: Severity::Low,
        }
    }
}

/// One usable segment: its epoch index, window and regularised values.
#[derive(Clone)]
struct Segment {
    k: i64,
    window: Window,
    values: Vec<f64>,
}

/// A judged segment that broke.
struct Broken {
    changed: bool,
    strength: f64,
    detected: Option<(i64, f64)>,
}

fn period_name(ns: i64) -> String {
    match ns {
        NS_PER_DAY => "daily".into(),
        x if x == 7 * NS_PER_DAY => "weekly".into(),
        x if x == 365 * NS_PER_DAY => "yearly".into(),
        x => format!("{}-period", format_duration(x)),
    }
}

fn capitalised(s: &str) -> String {
    let mut c = s.chars();
    c.next().map(|f| f.to_uppercase().chain(c).collect()).unwrap_or_default()
}

fn median(xs: &[f64]) -> Option<f64> {
    median_mad(xs).map(|(m, _)| m)
}

/// Epoch-anchored segments of `len_ns` over values regularised from `t0` in steps of
/// `step_ns`, keeping those with enough values.
fn cut(t0: i64, values: &[f64], step_ns: i64, len_ns: i64) -> Vec<Segment> {
    let n = values.len() as i64;
    if n == 0 || len_ns < step_ns {
        return Vec::new();
    }
    let bin = |ts: i64| ((ts - t0) + step_ns - 1).div_euclid(step_ns).clamp(0, n) as usize;
    let expected = (len_ns / step_ns) as f64;
    let (first_k, last_k) = (t0.div_euclid(len_ns), (t0 + (n - 1) * step_ns).div_euclid(len_ns));
    (first_k..=last_k)
        .filter_map(|k| {
            let (start, end) = (k * len_ns, (k + 1) * len_ns);
            let v = &values[bin(start)..bin(end)];
            let filled = v.iter().filter(|x| x.is_finite()).count() as f64;
            (filled >= MIN_COVERAGE * expected).then(|| Segment {
                k,
                window: Window::new(start, end),
                values: v.to_vec(),
            })
        })
        .collect()
}

impl SeasonalityBreak {
    fn validate(&self) -> Result<(Vec<i64>, Option<i64>)> {
        let invalid = |reason: &str| Error::InvalidParams { check: ID.into(), reason: reason.into() };
        if !(self.min_strength > 0.0 && self.min_strength <= 1.0) {
            return Err(invalid("min_strength must be in (0, 1]"));
        }
        if !(self.drop > 0.0 && self.drop < 1.0) {
            return Err(invalid("drop must be in (0, 1)"));
        }
        if self.ref_segments == 0 {
            return Err(invalid("ref_segments must be at least 1"));
        }
        let mut candidates = self
            .candidates
            .iter()
            .map(|c| duration_param(ID, "candidates", c))
            .collect::<Result<Vec<i64>>>()?;
        if candidates.is_empty() || candidates.iter().any(|&c| c <= 0) {
            return Err(invalid("candidates must be positive durations"));
        }
        candidates.sort_unstable();
        candidates.dedup();
        let segment =
            if self.segment == "auto" { None } else { Some(duration_param(ID, "segment", &self.segment)?) };
        if segment.is_some_and(|s| s <= 0) {
            return Err(invalid("segment must be positive"));
        }
        Ok((candidates, segment))
    }
}

impl Check for SeasonalityBreak {
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
        let (candidates, fixed_segment) = self.validate()?;
        let mut out = CheckOutput::default();
        let (f, _) = frame.normalized();
        let Some(step) = expected_interval(&f, ctx).filter(|&i| i > 0).map(|i| i.max(NS_PER_HOUR)) else {
            out.skipped.push((ID.into(), "expected interval".into()));
            return Ok(out);
        };
        let (t0, values) = regularise(&f, step);
        let bins = |p: i64| (p as f64 / step as f64).round() as usize;

        // The reference period: the shortest candidate the reference segments agree on.
        let mut shortest: Option<(i64, i64, Vec<Segment>)> = None;
        let mut chosen: Option<(i64, i64, Vec<Segment>)> = None;
        for &p in &candidates {
            let len = fixed_segment.unwrap_or((4 * p).max(7 * NS_PER_DAY));
            if bins(p) < 4 || len < 3 * p {
                continue; // too short a period for the step, or for the segment
            }
            let segments = cut(t0, &values, step, len);
            shortest.get_or_insert_with(|| (p, len, segments.clone()));
            if segments.len() < self.ref_segments {
                continue;
            }
            let fitting: Vec<i64> = candidates.iter().copied().filter(|&c| 3 * c <= len).collect();
            let agree = segments[..self.ref_segments]
                .iter()
                .filter(|s| detect_values(&s.values, step, &fitting).map(|d| d.0) == Some(p))
                .count();
            if 2 * agree >= self.ref_segments {
                chosen = Some((p, len, segments));
                break;
            }
        }

        let seasonal = chosen.is_some();
        let Some((p, len, segments)) = chosen.or(shortest) else {
            out.skipped
                .push((ID.into(), "insufficient data (shorter than 3 periods of any candidate)".into()));
            return Ok(out);
        };
        let strengths: Vec<Option<f64>> =
            segments.iter().map(|s| seasonal_strength(&s.values, bins(p))).collect();
        for (s, st) in segments.iter().zip(&strengths) {
            if let Some(v) = st {
                out.metrics.push(metric(ID, &f, "seasonal_strength", s.window.start, *v));
            }
        }
        if segments.len() <= self.ref_segments {
            out.skipped.push((
                ID.into(),
                format!(
                    "insufficient baseline ({} usable segments of {})",
                    segments.len(),
                    format_duration(len)
                ),
            ));
            return Ok(out);
        }
        if !seasonal {
            return Ok(out); // not seasonal: metrics only
        }
        let reference: Vec<f64> = strengths[..self.ref_segments].iter().flatten().copied().collect();
        let Some(strength_ref) = median(&reference) else { return Ok(out) };
        if strength_ref < self.min_strength {
            return Ok(out);
        }

        let fitting: Vec<i64> = candidates.iter().copied().filter(|&c| 3 * c <= len).collect();
        let mut broken: Vec<(usize, Window, Broken)> = Vec::new();
        for (s, st) in segments.iter().zip(&strengths).skip(self.ref_segments) {
            let Some(strength) = *st else { continue };
            let detected = detect_values(&s.values, step, &fitting);
            let changed = detected.is_some_and(|(q, sq)| q != p && sq >= self.min_strength);
            if changed || strength < (1.0 - self.drop) * strength_ref {
                broken.push((s.k as usize, s.window, Broken { changed, strength, detected }));
            }
        }

        let data_end = f.ts[f.len() - 1] + step;
        let name = period_name(p);
        for (w, items) in episodes(broken) {
            let w = Window::new(w.start.max(f.ts[0]), w.end.min(data_end));
            let n = items.len();
            let changed = items.iter().filter(|b| b.changed).count() * 2 >= n;
            let strength = items.iter().map(|b| b.strength).sum::<f64>() / n as f64;
            let new_periods: Vec<(i64, f64)> =
                items.iter().filter_map(|b| b.detected).filter(|&(q, _)| q != p).collect();
            let new_period = new_periods.first().map(|d| d.0);
            let new_strength = (!new_periods.is_empty())
                .then(|| new_periods.iter().map(|d| d.1).sum::<f64>() / new_periods.len() as f64);
            let dur = format_duration(w.duration());
            let summary = match (changed, new_period, new_strength) {
                (true, Some(q), Some(sq)) => format!(
                    "{} pattern turned {} for {dur} ({} strength {sq:.2}; {name} strength {strength:.2}, usually {strength_ref:.2})",
                    capitalised(&name),
                    period_name(q),
                    period_name(q),
                ),
                _ => format!("{} pattern weakened for {dur} (strength {strength:.2}, usually {strength_ref:.2})", capitalised(&name)),
            };
            out.findings.push(Finding::new(
                ID,
                &f.meta.id,
                Dimension::Plausibility,
                self.severity,
                w,
                ctx.window.overlap_fraction(&w),
                summary,
                serde_json::json!({
                    "period_ns": new_period, "period_ref_ns": p,
                    "strength": strength, "strength_ref": strength_ref, "strength_new": new_strength,
                    "reason": if changed { "period_changed" } else { "weaker" },
                    "n_segments": n, "segment_ns": len,
                }),
            ));
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::frame::SeriesMeta;
    use crate::synth::Rng;
    use crate::Profile;

    /// 2024-01-18T00:00Z, a multiple of 7 d and of 28 d since the epoch.
    const T0: i64 = 1_705_536_000 * 1_000_000_000;
    const H: usize = 24;
    const WEEK: usize = 7 * H;

    fn hourly(values: Vec<f64>) -> SeriesFrame {
        let ts = (0..values.len() as i64).map(|i| T0 + i * NS_PER_HOUR).collect();
        let mut meta = SeriesMeta::new("load");
        meta.name = Some("Feeder load".into());
        SeriesFrame::with_default_quality(meta, ts, values).unwrap()
    }

    /// Hour `i`: a daily sine, weekday/weekend levels, or flat, chosen per week.
    fn series(weeks: usize, seed: u64, shape: impl Fn(usize) -> &'static str) -> SeriesFrame {
        let mut rng = Rng::new(seed);
        let v = (0..weeks * WEEK)
            .map(|i| {
                let base = match shape(i / WEEK) {
                    "daily" => 50.0 + 10.0 * (i as f64 / H as f64 * std::f64::consts::TAU).sin(),
                    "weekly" => {
                        if (i / H) % 7 >= 5 {
                            30.0
                        } else {
                            60.0
                        }
                    }
                    _ => 50.0,
                };
                base + 1.0 * rng.normal()
            })
            .collect();
        hourly(v)
    }

    fn run(f: &SeriesFrame, params: serde_json::Value) -> CheckOutput {
        let check: SeasonalityBreak = serde_json::from_value(params).unwrap();
        check.run(f, &CheckContext::from_frame(f)).unwrap()
    }

    #[test]
    fn flat_week_flagged() {
        let f = series(6, 1, |w| if w == 4 { "flat" } else { "daily" });
        let out = run(&f, serde_json::json!({}));
        assert_eq!(out.findings.len(), 1, "{:?}", out.findings);
        let fi = &out.findings[0];
        let week = 7 * NS_PER_DAY;
        assert_eq!((fi.window.start, fi.window.end), (T0 + 4 * week, T0 + 5 * week));
        assert_eq!(fi.evidence["reason"], "weaker");
        assert_eq!(fi.evidence["period_ref_ns"], NS_PER_DAY);
        assert!(fi.evidence["strength"].as_f64().unwrap() < 0.3, "{}", fi.evidence);
        assert!(fi.evidence["strength_ref"].as_f64().unwrap() > 0.9, "{}", fi.evidence);
        assert!(fi.summary.starts_with("Daily pattern weakened for 7d (strength 0."), "{}", fi.summary);
        // One strength metric per weekly segment.
        assert_eq!(out.metrics.iter().filter(|m| m.name == "seasonal_strength").count(), 6);
    }

    #[test]
    fn period_change_flagged() {
        // Weekday/weekend levels for 20 weeks (28-day segments), then a daily shape only.
        let f = series(24, 2, |w| if w < 20 { "weekly" } else { "daily" });
        let out = run(&f, serde_json::json!({}));
        assert_eq!(out.findings.len(), 1, "{:?}", out.findings);
        let fi = &out.findings[0];
        assert_eq!(fi.evidence["reason"], "period_changed");
        assert_eq!(fi.evidence["period_ref_ns"], 7 * NS_PER_DAY);
        assert_eq!(fi.evidence["period_ns"], NS_PER_DAY);
        assert_eq!(fi.evidence["segment_ns"], 28 * NS_PER_DAY);
        assert!(fi.summary.starts_with("Weekly pattern turned daily for 28d"), "{}", fi.summary);
    }

    #[test]
    fn daily_to_weekly_needs_a_long_segment_to_name_the_new_period() {
        let f = series(24, 3, |w| if w < 20 { "daily" } else { "weekly" });
        // Seven-day segments cannot hold a weekly rhythm: the daily shape is simply gone.
        let out = run(&f, serde_json::json!({}));
        assert_eq!(out.findings.len(), 1, "{:?}", out.findings);
        assert_eq!(out.findings[0].evidence["reason"], "weaker");
        assert_eq!(out.findings[0].window.duration(), 28 * NS_PER_DAY);
        // Twenty-eight-day segments see the weekly rhythm.
        let out = run(&f, serde_json::json!({"segment": "28d"}));
        assert_eq!(out.findings.len(), 1, "{:?}", out.findings);
        assert_eq!(out.findings[0].evidence["reason"], "period_changed");
        assert_eq!(out.findings[0].evidence["period_ns"], 7 * NS_PER_DAY);
    }

    #[test]
    fn not_seasonal_is_silent() {
        // Wind-like: a slow AR(0.98) process has a high raw autocorrelation at one day.
        let mut rng = Rng::new(4);
        let mut x = 0.0;
        let f = hourly(
            (0..8 * WEEK)
                .map(|_| {
                    x = 0.98 * x + rng.normal();
                    10.0 + x
                })
                .collect(),
        );
        let out = run(&f, serde_json::json!({}));
        assert!(out.findings.is_empty(), "{:?}", out.findings);
        assert!(out.skipped.is_empty(), "{:?}", out.skipped);
        assert_eq!(out.metrics.len(), 8);
    }

    #[test]
    fn too_few_periods_skips() {
        let f = series(2, 5, |_| "daily");
        let out = run(&f, serde_json::json!({}));
        assert!(out.findings.is_empty());
        assert_eq!(out.skipped.len(), 1);
        assert!(
            out.skipped[0].1.starts_with("insufficient baseline (2 usable segments of 7d)"),
            "{:?}",
            out.skipped
        );
        // Two days of data: not even three periods of a day.
        let f = series(1, 5, |_| "daily");
        let short = SeriesFrame::with_default_quality(
            SeriesMeta::new("s"),
            f.ts[..2 * H].to_vec(),
            f.values[..2 * H].to_vec(),
        )
        .unwrap();
        let out = run(&short, serde_json::json!({}));
        assert!(out.skipped[0].1.starts_with("insufficient"), "{:?}", out.skipped);
    }

    #[test]
    fn self_profile_does_not_mask_break() {
        // The run's profile is computed from the same data, flat week included; the reference
        // comes from the leading segments regardless.
        let f = series(6, 1, |w| if w == 4 { "flat" } else { "daily" });
        let profile = Profile::compute(&f);
        assert_eq!(profile.dominant_period_ns, Some(NS_PER_DAY));
        let ctx = CheckContext::from_frame(&f).with_profile(profile);
        let out = SeasonalityBreak::default().run(&f, &ctx).unwrap();
        assert_eq!(out.findings.len(), 1, "{:?}", out.findings);
    }

    #[test]
    fn sparse_segment_not_judged() {
        // Week 5 is mostly missing: it is neither judged nor a finding.
        let mut f = series(6, 6, |_| "daily");
        for i in 4 * WEEK..4 * WEEK + 5 * H {
            f.values[i] = f64::NAN;
        }
        for i in 4 * WEEK + 5 * H..5 * WEEK {
            if i % 3 != 0 {
                f.values[i] = f64::NAN;
            }
        }
        let out = run(&f, serde_json::json!({}));
        assert!(out.findings.is_empty(), "{:?}", out.findings);
        assert_eq!(out.metrics.len(), 5);
    }

    #[test]
    fn bad_params_are_invalid() {
        let f = series(6, 1, |_| "daily");
        for (params, message) in [
            (serde_json::json!({"min_strength": 0.0}), "min_strength"),
            (serde_json::json!({"drop": 1.0}), "drop must be"),
            (serde_json::json!({"ref_segments": 0}), "ref_segments"),
            (serde_json::json!({"candidates": []}), "candidates must be"),
            (serde_json::json!({"candidates": ["daily"]}), "cannot parse duration"),
            (serde_json::json!({"segment": "0s"}), "segment must be positive"),
        ] {
            let check: SeasonalityBreak = serde_json::from_value(params).unwrap();
            let err = check.run(&f, &CheckContext::from_frame(&f)).unwrap_err();
            assert!(matches!(err, Error::InvalidParams { .. }) && err.to_string().contains(message), "{err}");
        }
    }

    #[test]
    fn registered_as_builtin() {
        use crate::registry::Registry;
        assert!(Registry::builtin_ids().contains(&ID));
        assert!(Registry::build(ID, &serde_json::Value::Null).is_ok());
    }
}
