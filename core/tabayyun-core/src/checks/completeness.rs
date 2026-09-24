//! `tby.completeness` — gaps and missing samples (catalogue #1).

use super::{duration_param, expected_interval, metric, Check, CheckContext, CheckOutput};
use crate::error::Result;
use crate::finding::{Dimension, Finding, Severity, Window};
use crate::frame::SeriesFrame;
use crate::time::format_duration;
use serde::{Deserialize, Serialize};

pub const ID: &str = "tby.completeness";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Completeness {
    /// A gap is an inter-arrival time larger than `gap_factor × expected_interval`.
    pub gap_factor: f64,
    /// Gaps shorter than this are ignored (human duration, e.g. "5m").
    pub min_gap: String,
    /// Below this completeness ratio over the window a summary finding is raised.
    pub min_completeness: f64,
    /// Count only usable-quality, finite samples as present.
    pub good_only: bool,
    /// Cap on per-gap findings; the rest are aggregated into the summary finding.
    pub max_gap_findings: usize,
    pub severity: Severity,
}

impl Default for Completeness {
    fn default() -> Self {
        Self {
            gap_factor: 3.0,
            min_gap: "5m".into(),
            min_completeness: 0.95,
            good_only: true,
            max_gap_findings: 200,
            severity: Severity::High,
        }
    }
}

impl Check for Completeness {
    fn id(&self) -> &'static str {
        ID
    }
    fn dimension(&self) -> Dimension {
        Dimension::Completeness
    }
    fn default_severity(&self) -> Severity {
        self.severity
    }

    fn run(&self, frame: &SeriesFrame, ctx: &CheckContext) -> Result<CheckOutput> {
        let mut out = CheckOutput::default();
        let (f, _) = frame.normalized();
        let Some(interval) = expected_interval(&f, ctx) else { return Ok(out) };
        let min_gap = duration_param(ID, "min_gap", &self.min_gap)?;
        let gap_threshold = ((self.gap_factor * interval as f64) as i64).max(min_gap);

        // Present samples: timestamps of samples that count as present.
        let present: Vec<i64> =
            f.ts.iter()
                .zip(&f.values)
                .zip(&f.quality)
                .filter(|((_, v), q)| !self.good_only || (q.is_usable() && v.is_finite()))
                .map(|((t, _), _)| *t)
                .collect();

        let win = ctx.window;
        let win_dur = win.duration().max(1) as f64;
        let mut gaps: Vec<(i64, i64)> = Vec::new();
        // Leading / trailing gaps relative to the evaluation window count too.
        let mut prev = win.start;
        for &t in present.iter().chain(std::iter::once(&win.end)) {
            if t.saturating_sub(prev) > gap_threshold {
                gaps.push((prev, t));
            }
            prev = prev.max(t);
        }
        let gap_total = gaps.iter().fold(0i64, |acc, (s, e)| acc.saturating_add(e.saturating_sub(*s)));
        let expected_n = (win_dur / interval as f64).round().max(1.0);
        let completeness = (present.iter().filter(|t| **t >= win.start && **t < win.end).count() as f64
            / expected_n)
            .min(1.0);
        out.metrics.push(metric(ID, &f, "completeness", win.end, completeness));
        out.metrics.push(metric(ID, &f, "gap_count", win.end, gaps.len() as f64));

        for (i, (s, e)) in gaps.iter().enumerate() {
            if i >= self.max_gap_findings {
                break;
            }
            let w = Window::new(*s, *e);
            let frac = w.duration() as f64 / win_dur;
            out.findings.push(Finding::new(
                ID,
                &f.meta.id,
                Dimension::Completeness,
                self.severity,
                w,
                frac,
                format!(
                    "No data for {} (expected a sample every {})",
                    format_duration(w.duration()),
                    format_duration(interval)
                ),
                serde_json::json!({
                    "gap_start": s, "gap_end": e, "gap_duration_ns": w.duration(),
                    "expected_interval_ns": interval, "gap_threshold_ns": gap_threshold,
                }),
            ));
        }
        if completeness < self.min_completeness {
            out.findings.push(Finding::new(
                ID,
                &f.meta.id,
                Dimension::Completeness,
                Severity::Medium,
                win,
                (1.0 - completeness).clamp(0.0, 1.0) * 0.5,
                format!(
                    "Completeness {:.1}% over the window (minimum {:.0}%), {} gaps totalling {}",
                    completeness * 100.0,
                    self.min_completeness * 100.0,
                    gaps.len(),
                    format_duration(gap_total)
                ),
                serde_json::json!({
                    "completeness": completeness, "min_completeness": self.min_completeness,
                    "gap_count": gaps.len(), "gap_total_ns": gap_total, "expected_samples": expected_n,
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

    #[test]
    fn clean_series_has_no_gaps() {
        let f = base(600);
        let out = Completeness::default().run(&f, &ctx(&f)).unwrap();
        assert!(ids(&out, ID).is_empty());
    }

    #[test]
    fn detects_injected_gap() {
        let mut f = base(600);
        let (s, e) = inject::gap(&mut f, 200, 30); // 30-minute hole
        let out = Completeness::default().run(&f, &ctx(&f)).unwrap();
        let g = ids(&out, ID);
        assert_eq!(g.len(), 1, "{:?}", out.findings);
        assert_eq!(g[0].window, Window::new(s - 60 * crate::time::NS_PER_SEC, e));
        assert!(g[0].score_impact > 0.0);
    }

    #[test]
    fn bad_quality_counts_as_missing() {
        let mut f = base(600);
        inject::quality(&mut f, 100, 60, crate::Quality::Bad);
        let out = Completeness::default().run(&f, &ctx(&f)).unwrap();
        let gaps: Vec<_> =
            ids(&out, ID).into_iter().filter(|x| x.evidence.get("gap_start").is_some()).collect();
        assert_eq!(gaps.len(), 1);
        let lax = Completeness { good_only: false, ..Default::default() };
        assert!(ids(&lax.run(&f, &ctx(&f)).unwrap(), ID).is_empty());
    }
}
