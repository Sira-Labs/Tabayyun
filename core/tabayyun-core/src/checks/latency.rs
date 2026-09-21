//! `tby.latency` — late arrival and future-stamped samples (catalogue #3).
//! Needs ingest timestamps on the frame (`SeriesFrame::ingest_ts`).

use super::{duration_param, expected_interval, metric, Check, CheckContext, CheckOutput};
use crate::error::{Error, Result};
use crate::finding::{Dimension, Finding, Severity, Window};
use crate::frame::SeriesFrame;
use crate::time::format_duration;
use serde::{Deserialize, Serialize};

pub const ID: &str = "tby.latency";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Latency {
    /// `"auto"` = 2 × expected interval; else a duration such as `"15m"`.
    pub sla: String,
    /// Event timestamps ahead of ingest by more than this are "future" samples.
    pub future_tolerance: String,
    pub severity: Severity,
}

impl Default for Latency {
    fn default() -> Self {
        Self { sla: "auto".into(), future_tolerance: "60s".into(), severity: Severity::Medium }
    }
}

impl Check for Latency {
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
        let Some(ingest) = &frame.ingest_ts else {
            return Err(Error::MissingMetadata { check: ID.into(), field: "ingest_ts" });
        };
        let mut out = CheckOutput::default();
        let n = frame.len();
        if n == 0 {
            return Ok(out);
        }
        let interval = expected_interval(frame, ctx);
        let future_tol = duration_param(ID, "future_tolerance", &self.future_tolerance)?;
        let sla = if self.sla == "auto" {
            match interval {
                Some(i) => 2 * i,
                None => return Ok(out),
            }
        } else {
            duration_param(ID, "sla", &self.sla)?
        };
        let mut lat: Vec<i64> = frame.ts.iter().zip(ingest).map(|(t, i)| i - t).collect();
        let future = lat.iter().filter(|l| **l < -future_tol).count();
        let max_lead = lat.iter().copied().min().unwrap_or(0).min(0).abs();
        lat.sort_unstable();
        let p95 = lat[((n - 1) as f64 * 0.95).round() as usize];
        let p50 = lat[(n - 1) / 2];
        out.metrics.push(metric(ID, frame, "latency_p50_ns", ctx.window.end, p50 as f64));
        out.metrics.push(metric(ID, frame, "latency_p95_ns", ctx.window.end, p95 as f64));
        out.metrics.push(metric(ID, frame, "future_samples", ctx.window.end, future as f64));

        if p95 > sla {
            let late = frame.ts.iter().zip(ingest).filter(|(t, i)| *i - *t > sla).count();
            out.findings.push(Finding::new(
                ID,
                &frame.meta.id,
                Dimension::Timeliness,
                self.severity,
                ctx.window,
                late as f64 / n as f64,
                format!(
                    "95th-percentile latency {} exceeds the {} SLA ({} of {} samples late)",
                    format_duration(p95),
                    format_duration(sla),
                    late,
                    n
                ),
                serde_json::json!({"p50_ns": p50, "p95_ns": p95, "sla_ns": sla, "late_samples": late}),
            ));
        }
        if future > 0 {
            let w = Window::new(ctx.window.start, ctx.window.end);
            out.findings.push(Finding::new(
                ID, &frame.meta.id, Dimension::Timeliness, Severity::High, w,
                future as f64 / n as f64,
                format!("{future} samples are stamped up to {} ahead of their arrival (clock ahead?)", format_duration(max_lead)),
                serde_json::json!({"future_samples": future, "max_lead_ns": max_lead, "future_tolerance_ns": future_tol}),
            ));
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::checks::testutil::*;
    use crate::time::NS_PER_MIN;

    #[test]
    fn skipped_without_ingest() {
        let f = base(100);
        assert!(matches!(Latency::default().run(&f, &ctx(&f)), Err(Error::MissingMetadata { .. })));
    }

    #[test]
    fn malformed_duration_is_an_error() {
        let f = base(10).with_ingest_ts(vec![0; 10]).unwrap();
        let bad = Latency { sla: "1h30m".into(), ..Default::default() };
        assert!(matches!(bad.run(&f, &ctx(&f)), Err(Error::InvalidParams { .. })));
    }

    #[test]
    fn late_and_future() {
        let f = base(200);
        let mut ing: Vec<i64> = f.ts.iter().map(|t| t + 5 * NS_PER_MIN).collect(); // 5 min late, SLA 2 min
        ing[10] = f.ts[10] - 10 * NS_PER_MIN; // future stamped
        let f = f.with_ingest_ts(ing).unwrap();
        let out = Latency::default().run(&f, &ctx(&f)).unwrap();
        let fs = ids(&out, ID);
        assert_eq!(fs.len(), 2, "{:?}", out.findings);
        assert!(fs.iter().any(|x| x.evidence["future_samples"] == 1));
    }
}
