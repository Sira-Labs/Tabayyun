//! Findings, metrics, severities and quality dimensions shared by all checks.

use serde::{Deserialize, Serialize};

/// Primary quality dimension of a check (see `docs/architecture/02-domain-model.md`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Dimension {
    Completeness,
    Timeliness,
    Validity,
    Accuracy,
    Consistency,
    Plausibility,
    Integrity,
}

impl Dimension {
    pub const ALL: [Dimension; 7] = [
        Dimension::Completeness,
        Dimension::Timeliness,
        Dimension::Validity,
        Dimension::Accuracy,
        Dimension::Consistency,
        Dimension::Plausibility,
        Dimension::Integrity,
    ];

    pub fn as_str(self) -> &'static str {
        match self {
            Dimension::Completeness => "completeness",
            Dimension::Timeliness => "timeliness",
            Dimension::Validity => "validity",
            Dimension::Accuracy => "accuracy",
            Dimension::Consistency => "consistency",
            Dimension::Plausibility => "plausibility",
            Dimension::Integrity => "integrity",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Severity {
    Low,
    Medium,
    High,
    Critical,
}

impl Severity {
    /// Weight used in `score_impact = weight × affected_fraction`
    /// (see `docs/checks/00-check-specification.md`).
    pub fn weight(self) -> f64 {
        match self {
            Severity::Critical => 1.0,
            Severity::High => 0.6,
            Severity::Medium => 0.3,
            Severity::Low => 0.1,
        }
    }
}

/// Half-open time window `[start, end)` in ns since epoch.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Window {
    pub start: i64,
    pub end: i64,
}

impl Window {
    pub fn new(start: i64, end: i64) -> Self {
        Self { start, end }
    }

    pub fn duration(&self) -> i64 {
        (self.end - self.start).max(0)
    }

    /// Fraction of `self` covered by `other`, clipped to [0, 1].
    pub fn overlap_fraction(&self, other: &Window) -> f64 {
        let d = self.duration();
        if d == 0 {
            return 0.0;
        }
        let s = self.start.max(other.start);
        let e = self.end.min(other.end);
        ((e - s).max(0) as f64 / d as f64).clamp(0.0, 1.0)
    }
}

/// One detected issue on one series.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Finding {
    pub check_id: String,
    pub series_id: String,
    pub dimension: Dimension,
    pub severity: Severity,
    pub window: Window,
    /// `severity.weight() × affected_fraction`, in [0, 1].
    pub score_impact: f64,
    /// Plain-language, evidence-filled sentence.
    pub summary: String,
    /// Machine-readable facts following the check's evidence schema.
    pub evidence: serde_json::Value,
}

impl Finding {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        check_id: &str,
        series_id: &str,
        dimension: Dimension,
        severity: Severity,
        window: Window,
        affected_fraction: f64,
        summary: impl Into<String>,
        evidence: serde_json::Value,
    ) -> Self {
        Self {
            check_id: check_id.to_string(),
            series_id: series_id.to_string(),
            dimension,
            severity,
            window,
            score_impact: (severity.weight() * affected_fraction.clamp(0.0, 1.0)).clamp(0.0, 1.0),
            summary: summary.into(),
            evidence,
        }
    }
}

/// A named scalar produced by a check for trending (e.g. `stale_age`, `completeness`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Metric {
    pub check_id: String,
    pub series_id: String,
    pub name: String,
    pub ts: i64,
    pub value: f64,
}
