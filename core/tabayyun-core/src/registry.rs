//! Check registry: builds checks from `(id, params)` pairs and runs a configured set.

use crate::checks::{self, Check, CheckContext, CheckOutput};
use crate::error::{Error, Result};
use crate::frame::SeriesFrame;
use serde::{Deserialize, Serialize};

/// One configured check: its id and JSON parameters (missing fields use defaults).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CheckConfig {
    pub id: String,
    #[serde(default)]
    pub params: serde_json::Value,
    #[serde(default = "default_true")]
    pub enabled: bool,
}

fn default_true() -> bool {
    true
}

pub struct Registry;

impl Registry {
    /// All built-in check ids in catalogue order.
    pub fn builtin_ids() -> &'static [&'static str] {
        &[
            checks::completeness::ID,
            checks::staleness::ID,
            checks::timestamp_integrity::ID,
            checks::sampling_regularity::ID,
            checks::value_type::ID,
            checks::flatline::ID,
            checks::physical_range::ID,
            checks::non_negative::ID,
            checks::quality_flags::ID,
            checks::operational_range::ID,
            checks::spikes::ID,
            checks::rate_of_change::ID,
            checks::resolution_loss::ID,
            checks::interpolation_artifacts::ID,
        ]
    }

    /// Instantiate a check from its id and parameters.
    pub fn build(id: &str, params: &serde_json::Value) -> Result<Box<dyn Check>> {
        fn parse<T: for<'de> Deserialize<'de> + Default + Check + 'static>(
            id: &str,
            p: &serde_json::Value,
        ) -> Result<Box<dyn Check>> {
            if p.is_null() {
                return Ok(Box::new(T::default()));
            }
            let t: T = serde_json::from_value(p.clone())
                .map_err(|e| Error::InvalidParams { check: id.to_string(), reason: e.to_string() })?;
            Ok(Box::new(t))
        }
        match id {
            checks::completeness::ID => parse::<checks::completeness::Completeness>(id, params),
            checks::staleness::ID => parse::<checks::staleness::Staleness>(id, params),
            checks::timestamp_integrity::ID => {
                parse::<checks::timestamp_integrity::TimestampIntegrity>(id, params)
            }
            checks::sampling_regularity::ID => {
                parse::<checks::sampling_regularity::SamplingRegularity>(id, params)
            }
            checks::value_type::ID => parse::<checks::value_type::ValueType>(id, params),
            checks::flatline::ID => parse::<checks::flatline::Flatline>(id, params),
            checks::physical_range::ID => parse::<checks::physical_range::PhysicalRange>(id, params),
            checks::non_negative::ID => parse::<checks::non_negative::NonNegative>(id, params),
            checks::quality_flags::ID => parse::<checks::quality_flags::QualityFlags>(id, params),
            checks::operational_range::ID => parse::<checks::operational_range::OperationalRange>(id, params),
            checks::spikes::ID => parse::<checks::spikes::Spikes>(id, params),
            checks::rate_of_change::ID => parse::<checks::rate_of_change::RateOfChange>(id, params),
            checks::resolution_loss::ID => parse::<checks::resolution_loss::ResolutionLoss>(id, params),
            checks::interpolation_artifacts::ID => {
                parse::<checks::interpolation_artifacts::InterpolationArtifacts>(id, params)
            }
            other => Err(Error::UnknownCheck(other.to_string())),
        }
    }

    /// Default configuration: every built-in check with default parameters.
    pub fn default_configs() -> Vec<CheckConfig> {
        Self::builtin_ids()
            .iter()
            .map(|id| CheckConfig { id: id.to_string(), params: serde_json::Value::Null, enabled: true })
            .collect()
    }

    /// Run the configured checks on one frame. Checks that need metadata the frame lacks
    /// are skipped and reported in `CheckOutput::skipped`.
    pub fn run(configs: &[CheckConfig], frame: &SeriesFrame, ctx: &CheckContext) -> Result<CheckOutput> {
        let mut out = CheckOutput::default();
        for cfg in configs.iter().filter(|c| c.enabled) {
            let check = Self::build(&cfg.id, &cfg.params)?;
            match check.run(frame, ctx) {
                Ok(o) => out.extend(o),
                Err(Error::MissingMetadata { check, field }) => out.skipped.push((check, field.to_string())),
                Err(e) => return Err(e),
            }
        }
        Ok(out)
    }
}
