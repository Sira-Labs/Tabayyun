//! `tby.flatline` — stuck / frozen values (catalogue #8).

use super::{duration_param, expected_interval, metric, run_window, Check, CheckContext, CheckOutput};
use crate::error::Result;
use crate::finding::{Dimension, Finding, Severity};
use crate::frame::{SeriesFrame, SeriesKind};
use crate::profile::resolution;
use crate::time::{format_duration, NS_PER_HOUR};
use serde::{Deserialize, Serialize};

pub const ID: &str = "tby.flatline";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Flatline {
    /// Minimum number of consecutive samples within `atol` to count as a run.
    pub min_run: usize,
    /// Minimum run duration ("auto" = max(1h, 10 × expected interval)).
    pub min_duration: String,
    /// Absolute tolerance; `None` = half the series resolution.
    pub atol: Option<f64>,
    /// Skip series whose baseline profile says they are mostly constant (setpoints, off-state).
    pub skip_constant_series: bool,
    /// Profile `constant_fraction` above which the series is considered legitimately constant.
    pub constant_fraction_limit: f64,
    /// Share of usable samples equal to the first one above which the whole frame is reported
    /// as frozen regardless of the profile (the profile may come from the frozen period itself).
    pub frozen_fraction: f64,
    pub severity: Severity,
}

impl Default for Flatline {
    fn default() -> Self {
        Self {
            min_run: 10,
            min_duration: "auto".into(),
            atol: None,
            skip_constant_series: true,
            constant_fraction_limit: 0.5,
            frozen_fraction: 0.99,
            severity: Severity::High,
        }
    }
}

impl Check for Flatline {
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
        if matches!(frame.meta.kind, SeriesKind::Setpoint | SeriesKind::Status) {
            return Ok(out);
        }
        let (f, _) = frame.normalized();
        let n = f.len();
        if n < self.min_run {
            return Ok(out);
        }
        let interval = expected_interval(&f, ctx).unwrap_or(0);
        if let Some(finding) = self.frozen_frame(&f, ctx, interval) {
            out.findings.push(finding);
            out.metrics.push(metric(ID, &f, "flat_fraction", ctx.window.end, 1.0));
            return Ok(out);
        }
        if self.skip_constant_series {
            if let Some(p) = &ctx.profile {
                if p.constant_fraction > self.constant_fraction_limit {
                    return Ok(out);
                }
            }
        }
        let min_duration = if self.min_duration == "auto" {
            (10 * interval).max(NS_PER_HOUR)
        } else {
            duration_param(ID, "min_duration", &self.min_duration)?
        };
        let atol = self
            .atol
            .or(frame.meta.resolution.map(|r| r / 2.0))
            .or(ctx.profile.as_ref().and_then(|p| p.resolution).map(|r| r / 2.0))
            .or(resolution(&f.values).map(|r| r / 2.0))
            .unwrap_or(0.0);

        // Runs of samples whose value stays within atol of the run's first value.
        let mut runs: Vec<(usize, usize)> = Vec::new();
        let mut s = 0usize;
        while s < n {
            if !f.values[s].is_finite() || !f.quality[s].is_usable() {
                s += 1;
                continue;
            }
            let anchor = f.values[s];
            let mut e = s + 1;
            while e < n
                && f.values[e].is_finite()
                && f.quality[e].is_usable()
                && (f.values[e] - anchor).abs() <= atol
            {
                e += 1;
            }
            if e - s >= self.min_run {
                runs.push((s, e));
            }
            s = e;
        }
        let mut flat_samples = 0usize;
        for (s, e) in runs {
            let w = run_window(&f, s, e, interval);
            if w.duration() < min_duration {
                continue;
            }
            flat_samples += e - s;
            out.findings.push(Finding::new(
                ID,
                &f.meta.id,
                Dimension::Plausibility,
                self.severity,
                w,
                ctx.window.overlap_fraction(&w),
                format!(
                    "Value stuck at {} for {} ({} samples)",
                    f.values[s],
                    format_duration(w.duration()),
                    e - s
                ),
                serde_json::json!({
                    "value": f.values[s], "run_samples": e - s, "run_duration_ns": w.duration(),
                    "atol": atol, "min_run": self.min_run, "min_duration_ns": min_duration,
                }),
            ));
        }
        out.metrics.push(metric(ID, &f, "flat_fraction", ctx.window.end, flat_samples as f64 / n as f64));
        Ok(out)
    }
}

impl Flatline {
    /// One finding covering the whole frame when (almost) every usable sample equals the first
    /// one: a sensor or historian tag that has stopped updating. Petrobras' 3W corpus reports
    /// 9.8 % of real well variables frozen for an entire instance, so this must not be masked
    /// by a "constant series" profile computed on the same period.
    fn frozen_frame(&self, f: &SeriesFrame, ctx: &CheckContext, interval: i64) -> Option<Finding> {
        let atol = self
            .atol
            .or(f.meta.resolution.map(|r| r / 2.0))
            .or(ctx.profile.as_ref().and_then(|p| p.resolution).map(|r| r / 2.0))
            .unwrap_or(0.0);
        let mut first: Option<f64> = None;
        let (mut usable, mut same) = (0usize, 0usize);
        for (v, q) in f.values.iter().zip(&f.quality) {
            if !v.is_finite() || !q.is_usable() {
                continue;
            }
            usable += 1;
            let anchor = *first.get_or_insert(*v);
            if (v - anchor).abs() <= atol {
                same += 1;
            }
        }
        if usable < self.min_run || (same as f64) < self.frozen_fraction * usable as f64 {
            return None;
        }
        let w = run_window(f, 0, f.len(), interval);
        Some(Finding::new(
            ID,
            &f.meta.id,
            Dimension::Plausibility,
            self.severity,
            w,
            ctx.window.overlap_fraction(&w),
            format!(
                "Series frozen at {} for the whole window ({}, {} of {} samples)",
                first.unwrap_or(f64::NAN),
                format_duration(w.duration()),
                same,
                usable
            ),
            serde_json::json!({
                "value": first, "run_samples": same, "usable_samples": usable,
                "run_duration_ns": w.duration(), "atol": atol, "frozen_fraction": self.frozen_fraction,
            }),
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::checks::testutil::*;
    use crate::synth::inject;
    use crate::Profile;

    #[test]
    fn clean_noise_not_flat() {
        let f = base(1440);
        assert!(ids(&Flatline::default().run(&f, &ctx(&f)).unwrap(), ID).is_empty());
    }

    #[test]
    fn detects_two_hour_flatline() {
        let mut f = base(1440);
        inject::flatline(&mut f, 300, 120);
        let out = Flatline::default().run(&f, &ctx(&f)).unwrap();
        let fs = ids(&out, ID);
        assert_eq!(fs.len(), 1);
        assert_eq!(fs[0].evidence["run_samples"], 120);
    }

    #[test]
    fn short_flat_ignored() {
        let mut f = base(1440);
        inject::flatline(&mut f, 300, 30); // 30 min < 1 h
        assert!(ids(&Flatline::default().run(&f, &ctx(&f)).unwrap(), ID).is_empty());
    }

    #[test]
    fn setpoint_skipped_but_frozen_measurement_reported_even_with_constant_profile() {
        let mut f = base(1440);
        inject::flatline(&mut f, 0, 1440);
        f.meta.kind = SeriesKind::Setpoint;
        assert!(ids(&Flatline::default().run(&f, &ctx(&f)).unwrap(), ID).is_empty());
        f.meta.kind = SeriesKind::Measurement;
        // Profile computed on the frozen period itself says "constant"; still one frozen finding.
        let c = ctx(&f).with_profile(Profile::compute(&f));
        let out = Flatline::default().run(&f, &c).unwrap();
        let fs = ids(&out, ID);
        assert_eq!(fs.len(), 1, "{fs:?}");
        assert_eq!(fs[0].evidence["usable_samples"], 1440);
        assert!(fs[0].summary.starts_with("Series frozen"));
        assert_eq!(ids(&Flatline::default().run(&f, &ctx(&f)).unwrap(), ID).len(), 1);
    }

    #[test]
    fn mostly_constant_series_with_constant_profile_skipped() {
        // An off-state signal that is constant 60 % of the time and noisy otherwise is
        // legitimately constant per its profile: no stuck findings.
        let mut f = base(1440);
        inject::flatline(&mut f, 100, 900);
        let c = ctx(&f).with_profile(Profile::compute(&f));
        assert!(ids(&Flatline::default().run(&f, &c).unwrap(), ID).is_empty());
        // Without a profile the 15 h run is reported as stuck.
        assert_eq!(ids(&Flatline::default().run(&f, &ctx(&f)).unwrap(), ID).len(), 1);
    }
}
