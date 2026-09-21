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
    /// Ignore runs resting on the series' floor (physical minimum, zero for non-negative
    /// quantities, else the observed minimum) when the floor is a recurring state.
    pub ignore_floor: bool,
    /// Share of usable samples that must sit on the floor for it to count as a recurring
    /// state (solar at night, a pump that is off) rather than a sensor stuck at its minimum.
    pub floor_fraction: f64,
    /// Fewer floor runs than this and the floor is not a recurring state: a series that is
    /// one long run at its minimum is reported as stuck.
    pub min_floor_runs: usize,
    /// A floor run longer than this multiple of the median floor run is still reported: a
    /// night that lasts three days is an outage, not idling.
    pub floor_run_factor: f64,
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
            ignore_floor: true,
            floor_fraction: 0.05,
            min_floor_runs: 3,
            floor_run_factor: 3.0,
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
        let floor = if self.ignore_floor { resting_floor(&f, atol, self.floor_fraction) } else { None };

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
        let runs: Vec<(usize, usize)> = runs
            .into_iter()
            .filter(|&(s, e)| run_window(&f, s, e, interval).duration() >= min_duration)
            .collect();
        let on_floor = |s: usize| floor.is_some_and(|fl| (f.values[s] - fl).abs() <= atol);
        // Usual duration of a floor run; much longer ones are outages rather than idling. The
        // floor only counts as a recurring state once enough runs rest on it.
        let mut floor_durations: Vec<i64> = runs
            .iter()
            .filter(|&&(s, _)| on_floor(s))
            .map(|&(s, e)| run_window(&f, s, e, interval).duration())
            .collect();
        floor_durations.sort_unstable();
        let usual_floor_run = (floor_durations.len() >= self.min_floor_runs.max(1))
            .then(|| floor_durations[floor_durations.len() / 2])
            .map(|d| (d as f64 * self.floor_run_factor) as i64);
        let mut flat_samples = 0usize;
        let mut floor_runs = 0usize;
        for (s, e) in runs {
            let w = run_window(&f, s, e, interval);
            let resting = on_floor(s);
            if resting && usual_floor_run.is_some_and(|max| w.duration() <= max) {
                floor_runs += 1;
                continue;
            }
            flat_samples += e - s;
            let summary = if resting {
                format!(
                    "Value stuck on the floor {} for {} ({} samples), far longer than the usual resting run",
                    f.values[s],
                    format_duration(w.duration()),
                    e - s
                )
            } else {
                format!(
                    "Value stuck at {} for {} ({} samples)",
                    f.values[s],
                    format_duration(w.duration()),
                    e - s
                )
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
                    "value": f.values[s], "run_samples": e - s, "run_duration_ns": w.duration(),
                    "atol": atol, "min_run": self.min_run, "min_duration_ns": min_duration,
                    "floor": floor, "on_floor": resting, "max_floor_run_ns": usual_floor_run,
                }),
            ));
        }
        out.metrics.push(metric(ID, &f, "flat_fraction", ctx.window.end, flat_samples as f64 / n as f64));
        out.metrics.push(metric(ID, &f, "floor_runs", ctx.window.end, floor_runs as f64));
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

/// The value the series rests on when it is legitimately idle, if such a state exists.
///
/// The candidate floor is the physical minimum (explicit or unit-inferred), else zero for a
/// non-negative quantity, else the observed minimum. It only counts as a resting state when
/// at least `min_fraction` of the usable samples sit on it: a solar plant reads exactly zero
/// every night, whereas a sensor stuck at its minimum produces one long run in an otherwise
/// varying series and so never reaches the share.
fn resting_floor(f: &SeriesFrame, atol: f64, min_fraction: f64) -> Option<f64> {
    let usable = |i: usize| f.values[i].is_finite() && f.quality[i].is_usable();
    let n_usable = (0..f.len()).filter(|&i| usable(i)).count();
    if n_usable == 0 {
        return None;
    }
    let observed_min = (0..f.len()).filter(|&i| usable(i)).map(|i| f.values[i]).fold(f64::INFINITY, f64::min);
    let candidate = f
        .meta
        .physical_limits()
        .0
        .or(if f.meta.is_non_negative() { Some(0.0) } else { None })
        .unwrap_or(observed_min);
    let on_floor = (0..f.len()).filter(|&i| usable(i) && (f.values[i] - candidate).abs() <= atol).count();
    (on_floor as f64 / n_usable as f64 >= min_fraction).then_some(candidate)
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

    /// Solar-like series: a 1-minute sinusoid clipped at zero, so every "night" is a run of
    /// zeros that lasts hours.
    fn generation_like(days: usize) -> SeriesFrame {
        let mut f = base(days * 1440);
        for v in f.values.iter_mut() {
            *v = (*v - 50.0).max(0.0).round();
        }
        f
    }

    #[test]
    fn nightly_zero_runs_on_generation_series_ignored() {
        let f = generation_like(7);
        let out = Flatline::default().run(&f, &ctx(&f)).unwrap();
        assert!(ids(&out, ID).is_empty(), "{:?}", out.findings);
        let floor_runs = out.metrics.iter().find(|m| m.name == "floor_runs").unwrap().value;
        assert!(floor_runs >= 6.0, "floor runs {floor_runs}");
        // Opting out restores one finding per night.
        let strict = Flatline { ignore_floor: false, ..Flatline::default() };
        assert_eq!(ids(&strict.run(&f, &ctx(&f)).unwrap(), ID).len() as f64, floor_runs);
    }

    #[test]
    fn floor_run_much_longer_than_a_night_is_reported() {
        let mut f = generation_like(10);
        inject::set(&mut f, 3 * 1440, 3 * 1440, 0.0); // three dark days in a row
        let out = Flatline::default().run(&f, &ctx(&f)).unwrap();
        let fs = ids(&out, ID);
        assert_eq!(fs.len(), 1, "{:?}", out.findings);
        assert_eq!(fs[0].evidence["on_floor"], true);
        assert!(fs[0].evidence["run_samples"].as_u64().unwrap() >= 3 * 1440);
    }

    #[test]
    fn stuck_at_minimum_of_a_varying_series_still_flagged() {
        let mut f = base(7 * 1440);
        let min = f.values.iter().cloned().fold(f64::INFINITY, f64::min);
        inject::set(&mut f, 3000, 180, min); // 3 h at the observed minimum: 1.8 % of samples
        let out = Flatline::default().run(&f, &ctx(&f)).unwrap();
        let fs = ids(&out, ID);
        assert_eq!(fs.len(), 1, "{:?}", out.findings);
        assert_eq!(fs[0].evidence["on_floor"], false);
    }

    /// A setpoint is never stuck; a measurement frozen for the whole frame always is.
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

    /// A series that is constant most of the time but not frozen is legitimately constant.
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
