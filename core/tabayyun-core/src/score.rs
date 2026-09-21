//! Scoring v1 (see `docs/architecture/02-domain-model.md`).
//!
//! Series dimension score = 100 × (1 − clip(Σ score_impact of findings in that dimension)).
//! Overall = weighted mean of dimension scores. Weights are per workspace; defaults below.

use crate::finding::{Dimension, Finding};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

pub const METHOD_VERSION: &str = "v1";

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Scorer {
    /// Relative weights per dimension for the overall score.
    pub weights: BTreeMap<Dimension, f64>,
}

impl Default for Scorer {
    fn default() -> Self {
        let mut weights = BTreeMap::new();
        weights.insert(Dimension::Completeness, 1.0);
        weights.insert(Dimension::Timeliness, 1.0);
        weights.insert(Dimension::Validity, 1.5);
        weights.insert(Dimension::Accuracy, 1.0);
        weights.insert(Dimension::Consistency, 1.0);
        weights.insert(Dimension::Plausibility, 1.0);
        weights.insert(Dimension::Integrity, 1.0);
        Self { weights }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ScoreReport {
    pub series_id: String,
    pub method_version: String,
    pub overall: f64,
    pub dimensions: BTreeMap<Dimension, f64>,
    pub n_findings: usize,
}

impl Scorer {
    pub fn score(&self, series_id: &str, findings: &[Finding]) -> ScoreReport {
        let mut impact: BTreeMap<Dimension, f64> = BTreeMap::new();
        for f in findings.iter().filter(|f| f.series_id == series_id) {
            *impact.entry(f.dimension).or_insert(0.0) += f.score_impact;
        }
        let mut dimensions = BTreeMap::new();
        let mut num = 0.0;
        let mut den = 0.0;
        for d in Dimension::ALL {
            let s = 100.0 * (1.0 - impact.get(&d).copied().unwrap_or(0.0).clamp(0.0, 1.0));
            let s = (s * 10.0).round() / 10.0;
            dimensions.insert(d, s);
            let w = self.weights.get(&d).copied().unwrap_or(1.0);
            num += w * s;
            den += w;
        }
        ScoreReport {
            series_id: series_id.to_string(),
            method_version: METHOD_VERSION.into(),
            overall: if den > 0.0 { ((num / den) * 10.0).round() / 10.0 } else { 100.0 },
            dimensions,
            n_findings: findings.iter().filter(|f| f.series_id == series_id).count(),
        }
    }
}

impl PartialOrd for Dimension {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for Dimension {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        (*self as u8).cmp(&(*other as u8))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::finding::{Severity, Window};

    #[test]
    fn scores_clip_and_weight() {
        let f = Finding::new(
            "tby.x",
            "s",
            Dimension::Validity,
            Severity::Critical,
            Window::new(0, 10),
            0.5,
            "x",
            serde_json::json!({}),
        );
        let r = Scorer::default().score("s", &[f.clone(), f]);
        assert_eq!(r.dimensions[&Dimension::Validity], 0.0);
        assert_eq!(r.dimensions[&Dimension::Completeness], 100.0);
        assert!(r.overall < 100.0 && r.overall > 70.0);
        assert_eq!(r.n_findings, 2);
    }
}
