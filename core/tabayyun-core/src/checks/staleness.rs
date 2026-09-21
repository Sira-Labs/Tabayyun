//! `tby.staleness` — series not updating (catalogue #2).

use super::{duration_param, expected_interval, metric, Check, CheckContext, CheckOutput};
use crate::error::Result;
use crate::finding::{Dimension, Finding, Severity, Window};
use crate::frame::SeriesFrame;
use crate::time::format_duration;
use serde::{Deserialize, Serialize};

pub const ID: &str = "tby.staleness";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Staleness {
    /// `"auto"` = max(3 × expected_interval, min_age); or a duration such as `"4h"`.
    pub max_age: String,
    pub min_age: String,
    pub ignore_quality_bad: bool,
    pub severity: Severity,
}

impl Default for Staleness {
    fn default() -> Self {
        Self {
            max_age: "auto".into(),
            min_age: "5m".into(),
            ignore_quality_bad: true,
            severity: Severity::High,
        }
    }
}

impl Staleness {
    fn threshold(&self, interval: Option<i64>) -> Result<Option<i64>> {
        let min_age = duration_param(ID, "min_age", &self.min_age)?;
        if self.max_age == "auto" {
            Ok(interval.map(|i| (3 * i).max(min_age)))
        } else {
            duration_param(ID, "max_age", &self.max_age).map(Some)
        }
    }
}

impl Check for Staleness {
    fn id(&self) -> &'static str {
        ID
    }
    fn dimension(&self) -> Dimension {
        Dimension::Timeliness
    }
    fn default_severity(&self) -> Severity {
        self.severity
    }

    fn run(&self, frame: &SeriesFrame, ctx: &CheckContext) -> Result<CheckOutput> {
        let mut out = CheckOutput::default();
        let interval = expected_interval(frame, ctx);
        let Some(max_age) = self.threshold(interval)? else { return Ok(out) };
        let newest = if self.ignore_quality_bad { frame.last_good_ts() } else { frame.last_ts() };
        let age = match newest {
            Some(t) => ctx.now_ns - t,
            None => ctx.window.duration(),
        };
        out.metrics.push(metric(ID, frame, "stale_age_ns", ctx.now_ns, age as f64));
        if age > max_age {
            let stale_from = newest.unwrap_or(ctx.window.start);
            let w = Window::new(stale_from.max(ctx.window.start), ctx.now_ns.max(stale_from + 1));
            let frac = w.duration() as f64 / ctx.window.duration().max(1) as f64;
            out.findings.push(Finding::new(
                ID,
                &frame.meta.id,
                Dimension::Timeliness,
                self.severity,
                w,
                frac,
                match (newest, interval) {
                    (Some(_), Some(i)) => format!(
                        "Newest good sample is {} old (expected every {}, limit {})",
                        format_duration(age),
                        format_duration(i),
                        format_duration(max_age)
                    ),
                    (Some(_), None) => format!(
                        "Newest good sample is {} old (limit {})",
                        format_duration(age),
                        format_duration(max_age)
                    ),
                    (None, _) => "No good samples at all in the window".to_string(),
                },
                serde_json::json!({
                    "newest_ts": newest, "age_ns": age, "max_age_ns": max_age,
                    "expected_interval_ns": interval, "now_ns": ctx.now_ns,
                }),
            ));
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::checks::testutil::*;
    use crate::synth::inject;
    use crate::time::NS_PER_HOUR;

    #[test]
    fn fresh_series_passes() {
        let f = base(100);
        let out = Staleness::default().run(&f, &ctx(&f)).unwrap();
        assert!(ids(&out, ID).is_empty());
    }

    #[test]
    fn stale_when_now_moves_on() {
        let f = base(100);
        let c = ctx(&f).with_now(f.last_ts().unwrap() + 2 * NS_PER_HOUR);
        let out = Staleness::default().run(&f, &c).unwrap();
        let s = ids(&out, ID);
        assert_eq!(s.len(), 1);
        assert_eq!(s[0].evidence["max_age_ns"], serde_json::json!(5 * crate::time::NS_PER_MIN));
    }

    #[test]
    fn bad_quality_tail_is_stale() {
        let mut f = base(100);
        inject::quality(&mut f, 80, 20, crate::Quality::Bad);
        let out = Staleness::default().run(&f, &ctx(&f)).unwrap();
        assert_eq!(ids(&out, ID).len(), 1);
    }
}
