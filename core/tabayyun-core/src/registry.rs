//! Check registry: builds checks from `(id, params)` pairs and runs a configured set, on one
//! series ([`Registry::run`]) or on several series and their groups ([`Registry::run_multi`]).

use crate::checks::{self, Check, CheckContext, CheckOutput};
use crate::cross::{CrossCheck, GroupSkip, SeriesGroup};
use crate::error::{Error, Result};
use crate::frame::SeriesFrame;
use crate::profile::Profile;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashMap};

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

/// Result of [`Registry::run_multi`]: one output per series id (cross-series findings land on
/// the member they name) and the groups that could not run.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct MultiOutput {
    pub per_series: BTreeMap<String, CheckOutput>,
    pub groups_skipped: Vec<GroupSkip>,
}

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
            checks::latency::ID,
            checks::scale_shift::ID,
            checks::noise_level::ID,
            checks::level_drift::ID,
            checks::distribution_drift::ID,
            checks::changepoint::ID,
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
            checks::latency::ID => parse::<checks::latency::Latency>(id, params),
            checks::scale_shift::ID => parse::<checks::scale_shift::ScaleShift>(id, params),
            checks::noise_level::ID => parse::<checks::noise_level::NoiseLevel>(id, params),
            checks::level_drift::ID => parse::<checks::level_drift::LevelDrift>(id, params),
            checks::distribution_drift::ID => {
                parse::<checks::distribution_drift::DistributionDrift>(id, params)
            }
            checks::changepoint::ID => parse::<checks::changepoint::Changepoint>(id, params),
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

    /// Built-in cross-series check ids in catalogue order.
    pub fn cross_builtin_ids() -> &'static [&'static str] {
        &[checks::correlation_break::ID, checks::redundant_disagreement::ID, checks::balance_residual::ID]
    }

    /// Instantiate a cross-series check from its id and parameters.
    pub fn build_cross(id: &str, params: &serde_json::Value) -> Result<Box<dyn CrossCheck>> {
        fn parse<T: for<'de> Deserialize<'de> + Default + CrossCheck + 'static>(
            id: &str,
            p: &serde_json::Value,
        ) -> Result<Box<dyn CrossCheck>> {
            if p.is_null() {
                return Ok(Box::new(T::default()));
            }
            let t: T = serde_json::from_value(p.clone())
                .map_err(|e| Error::InvalidParams { check: id.to_string(), reason: e.to_string() })?;
            Ok(Box::new(t))
        }
        match id {
            checks::correlation_break::ID => parse::<checks::correlation_break::CorrelationBreak>(id, params),
            checks::redundant_disagreement::ID => {
                parse::<checks::redundant_disagreement::RedundantDisagreement>(id, params)
            }
            checks::balance_residual::ID => parse::<checks::balance_residual::BalanceResidual>(id, params),
            other => Err(Error::UnknownCheck(other.to_string())),
        }
    }

    /// Default configuration for multi-series runs: every single-series and cross check.
    pub fn default_multi_configs() -> Vec<CheckConfig> {
        let mut configs = Self::default_configs();
        configs.extend(Self::cross_builtin_ids().iter().map(|id| CheckConfig {
            id: id.to_string(),
            params: serde_json::Value::Null,
            enabled: true,
        }));
        configs
    }

    /// Run single-series checks on every frame and cross-series checks on every group whose
    /// members all have data. `configs` may mix single-series and cross-series check ids.
    ///
    /// Each frame is checked with `ctx` plus its entry in `profiles` (keyed by series id), so a
    /// series gets the same findings as a single-series run with that profile; frames without
    /// an entry use `ctx` as given.
    pub fn run_multi(
        configs: &[CheckConfig],
        frames: &[SeriesFrame],
        profiles: &BTreeMap<String, Profile>,
        groups: &[SeriesGroup],
        ctx: &CheckContext,
    ) -> Result<MultiOutput> {
        let cross_ids = Self::cross_builtin_ids();
        let (cross_configs, single): (Vec<CheckConfig>, Vec<CheckConfig>) =
            configs.iter().cloned().partition(|c| cross_ids.contains(&c.id.as_str()));
        let cross = cross_configs
            .iter()
            .filter(|c| c.enabled)
            .map(|c| Self::build_cross(&c.id, &c.params))
            .collect::<Result<Vec<_>>>()?;
        Self::run_multi_with(&single, &cross, frames, profiles, groups, ctx)
    }

    /// [`Registry::run_multi`] with the cross-series checks supplied by the caller.
    ///
    /// Frame ids must be unique. A group is skipped (and reported) when a member has no frame
    /// or an empty one; otherwise every cross check whose kinds include the group's kind runs
    /// on the members in declaration order. A cross check that lacks metadata is reported as
    /// skipped on the group's first member.
    pub fn run_multi_with(
        configs: &[CheckConfig],
        cross: &[Box<dyn CrossCheck>],
        frames: &[SeriesFrame],
        profiles: &BTreeMap<String, Profile>,
        groups: &[SeriesGroup],
        ctx: &CheckContext,
    ) -> Result<MultiOutput> {
        let mut out = MultiOutput::default();
        let mut by_id: HashMap<&str, &SeriesFrame> = HashMap::with_capacity(frames.len());
        for frame in frames {
            if by_id.insert(frame.meta.id.as_str(), frame).is_some() {
                return Err(Error::InvalidFrame(format!("series {} appears twice", frame.meta.id)));
            }
            let output = match profiles.get(&frame.meta.id) {
                Some(p) => Self::run(configs, frame, &ctx.clone().with_profile(p.clone()))?,
                None => Self::run(configs, frame, ctx)?,
            };
            out.per_series.insert(frame.meta.id.clone(), output);
        }
        for group in groups {
            group.validate()?;
            let missing: Vec<String> = group
                .member_ids()
                .filter(|id| by_id.get(id).is_none_or(|f| f.is_empty()))
                .map(str::to_string)
                .collect();
            if !missing.is_empty() {
                out.groups_skipped.push(GroupSkip {
                    group_id: group.id.clone(),
                    reason: "members without data".into(),
                    missing,
                });
                continue;
            }
            let members: Vec<&SeriesFrame> = group.member_ids().map(|id| by_id[id]).collect();
            let first = members[0].meta.id.as_str();
            for check in cross.iter().filter(|c| c.kinds().contains(&group.kind)) {
                let result = match check.run(&members, group, ctx) {
                    Ok(result) => result,
                    Err(Error::MissingMetadata { check, field }) => {
                        CheckOutput { skipped: vec![(check, field.to_string())], ..Default::default() }
                    }
                    Err(e) => return Err(e),
                };
                route(&mut out.per_series, group, first, result)?;
            }
        }
        Ok(out)
    }
}

/// File a cross check's output under the member series it names; skips go to `first`.
fn route(
    per_series: &mut BTreeMap<String, CheckOutput>,
    group: &SeriesGroup,
    first: &str,
    result: CheckOutput,
) -> Result<()> {
    let outside = |check: &str, series: &str| {
        Error::InvalidFrame(format!("{check} reported series {series}, which is not in group {}", group.id))
    };
    for finding in result.findings {
        let target = per_series
            .get_mut(&finding.series_id)
            .filter(|_| group.member_ids().any(|m| m == finding.series_id));
        target.ok_or_else(|| outside(&finding.check_id, &finding.series_id))?.findings.push(finding);
    }
    for metric in result.metrics {
        let target = per_series
            .get_mut(&metric.series_id)
            .filter(|_| group.member_ids().any(|m| m == metric.series_id));
        target.ok_or_else(|| outside(&metric.check_id, &metric.series_id))?.metrics.push(metric);
    }
    if let Some(first) = per_series.get_mut(first) {
        first.skipped.extend(result.skipped);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cross::{GroupKind, GroupMember, MemberRole};
    use crate::finding::{Dimension, Finding, Severity, Window};
    use crate::frame::SeriesMeta;

    const HOUR: i64 = 3_600_000_000_000;

    /// Emits one finding per group on the member named by `target` (default: first member).
    struct Fake {
        target: Option<&'static str>,
        needs_metadata: bool,
    }

    impl CrossCheck for Fake {
        fn id(&self) -> &'static str {
            "test.fake_cross"
        }
        fn dimension(&self) -> Dimension {
            Dimension::Consistency
        }
        fn default_severity(&self) -> Severity {
            Severity::Medium
        }
        fn kinds(&self) -> &'static [GroupKind] {
            &[GroupKind::Redundant]
        }
        fn run(
            &self,
            frames: &[&SeriesFrame],
            group: &SeriesGroup,
            _ctx: &CheckContext,
        ) -> Result<CheckOutput> {
            if self.needs_metadata {
                return Err(Error::MissingMetadata { check: self.id().into(), field: "unit" });
            }
            let series = self.target.unwrap_or(frames[0].meta.id.as_str());
            let evidence = serde_json::Value::Object(group.evidence_base());
            let window = Window::new(frames[0].ts[0], frames[0].ts[0] + HOUR);
            let finding = Finding::new(
                self.id(),
                series,
                self.dimension(),
                self.default_severity(),
                window,
                0.1,
                "fake",
                evidence,
            );
            Ok(CheckOutput { findings: vec![finding], ..Default::default() })
        }
    }

    fn frame(id: &str, n: i64) -> SeriesFrame {
        let ts: Vec<i64> = (0..n).map(|i| 1_700_000_000_000_000_000 / HOUR * HOUR + i * HOUR).collect();
        let values = (0..n).map(|i| 20.0 + (i % 24) as f64).collect();
        SeriesFrame::with_default_quality(SeriesMeta::new(id), ts, values).unwrap()
    }

    fn group(id: &str, kind: GroupKind, members: &[&str]) -> SeriesGroup {
        let role = |i: usize| match kind {
            GroupKind::Balance if i == 0 => MemberRole::Input,
            GroupKind::Balance => MemberRole::Output,
            _ => MemberRole::Member,
        };
        SeriesGroup {
            id: id.into(),
            name: id.to_uppercase(),
            kind,
            members: members
                .iter()
                .enumerate()
                .map(|(i, s)| GroupMember { series_id: s.to_string(), role: role(i) })
                .collect(),
            params: serde_json::Value::Null,
        }
    }

    fn none() -> BTreeMap<String, Profile> {
        BTreeMap::new()
    }

    fn cross(target: Option<&'static str>) -> Vec<Box<dyn CrossCheck>> {
        vec![Box::new(Fake { target, needs_metadata: false })]
    }

    #[test]
    fn run_multi_runs_single_checks_per_frame_and_cross_checks_per_matching_group() {
        let frames = vec![frame("a", 48), frame("b", 48), frame("c", 48)];
        let ctx = CheckContext::from_frame(&frames[0]);
        let groups = vec![
            group("g1", GroupKind::Redundant, &["a", "b"]),
            group("g2", GroupKind::Balance, &["b", "c"]),
        ];
        let configs = Registry::default_configs();
        let out = Registry::run_multi_with(&configs, &cross(None), &frames, &none(), &groups, &ctx).unwrap();
        assert_eq!(out.per_series.keys().collect::<Vec<_>>(), vec!["a", "b", "c"]);
        assert!(out.groups_skipped.is_empty());
        // The fake check applies to redundant groups only, and names the first member.
        let fake =
            |s: &str| out.per_series[s].findings.iter().filter(|f| f.check_id == "test.fake_cross").count();
        assert_eq!((fake("a"), fake("b"), fake("c")), (1, 0, 0));
        let evidence =
            &out.per_series["a"].findings.iter().find(|f| f.check_id == "test.fake_cross").unwrap().evidence;
        assert_eq!(evidence["group_id"], "g1");
        // Single-series checks ran on every frame, like Registry::run.
        let single = Registry::run(&configs, &frames[2], &ctx).unwrap();
        assert_eq!(out.per_series["c"], single);
    }

    #[test]
    fn run_multi_uses_each_series_profile_like_a_single_run() {
        let frames = vec![frame("a", 96), frame("b", 96)];
        let ctx = CheckContext::from_frame(&frames[0]);
        let profiles: BTreeMap<String, Profile> =
            frames.iter().map(|f| (f.meta.id.clone(), Profile::compute(f))).collect();
        let configs = Registry::default_configs();
        let out = Registry::run_multi(&configs, &frames, &profiles, &[], &ctx).unwrap();
        for f in &frames {
            let single = Registry::run(&configs, f, &ctx.clone().with_profile(profiles[&f.meta.id].clone()));
            assert_eq!(out.per_series[&f.meta.id], single.unwrap());
        }
    }

    #[test]
    fn run_multi_skips_and_reports_groups_with_members_lacking_data() {
        let frames = vec![frame("a", 24), frame("b", 24), frame("empty", 0)];
        let ctx = CheckContext::from_frame(&frames[0]);
        let groups = vec![
            group("g-absent", GroupKind::Redundant, &["a", "x", "y"]),
            group("g-empty", GroupKind::Redundant, &["empty", "b"]),
            group("g-ok", GroupKind::Redundant, &["b", "a"]),
        ];
        let out = Registry::run_multi_with(&[], &cross(None), &frames, &none(), &groups, &ctx).unwrap();
        let skipped: Vec<(&str, Vec<&str>)> = out
            .groups_skipped
            .iter()
            .map(|s| (s.group_id.as_str(), s.missing.iter().map(String::as_str).collect()))
            .collect();
        assert_eq!(skipped, vec![("g-absent", vec!["x", "y"]), ("g-empty", vec!["empty"])]);
        assert!(out.groups_skipped.iter().all(|s| s.reason == "members without data"));
        assert_eq!(out.per_series["b"].findings.len(), 1);
        assert!(out.per_series["a"].findings.is_empty());
    }

    #[test]
    fn run_multi_reports_missing_metadata_on_the_first_member() {
        let frames = vec![frame("a", 24), frame("b", 24)];
        let ctx = CheckContext::from_frame(&frames[0]);
        let checks: Vec<Box<dyn CrossCheck>> = vec![Box::new(Fake { target: None, needs_metadata: true })];
        let groups = vec![group("g", GroupKind::Redundant, &["b", "a"])];
        let out = Registry::run_multi_with(&[], &checks, &frames, &none(), &groups, &ctx).unwrap();
        assert_eq!(out.per_series["b"].skipped, vec![("test.fake_cross".to_string(), "unit".to_string())]);
        assert!(out.per_series["a"].skipped.is_empty());
    }

    #[test]
    fn run_multi_rejects_duplicates_invalid_groups_foreign_series_and_unknown_checks() {
        let ctx = CheckContext::from_frame(&frame("a", 24));
        let twice = vec![frame("a", 24), frame("a", 24)];
        let err = Registry::run_multi_with(&[], &[], &twice, &none(), &[], &ctx).unwrap_err();
        assert!(err.to_string().contains("appears twice"));

        let frames = vec![frame("a", 24), frame("b", 24), frame("c", 24)];
        let lonely = vec![group("g", GroupKind::Redundant, &["a"])];
        let err = Registry::run_multi_with(&[], &[], &frames, &none(), &lonely, &ctx).unwrap_err();
        assert!(matches!(err, Error::InvalidGroup { .. }));

        // A cross check may only name a member of the group it ran on.
        let groups = vec![group("g", GroupKind::Redundant, &["a", "b"])];
        let err =
            Registry::run_multi_with(&[], &cross(Some("c")), &frames, &none(), &groups, &ctx).unwrap_err();
        assert!(err.to_string().contains("not in group g"));

        let unknown =
            vec![CheckConfig { id: "tby.nope".into(), params: serde_json::Value::Null, enabled: true }];
        assert!(matches!(
            Registry::run_multi(&unknown, &frames, &none(), &groups, &ctx),
            Err(Error::UnknownCheck(_))
        ));
    }

    #[test]
    fn default_multi_configs_cover_single_and_cross_checks() {
        let ids: Vec<String> = Registry::default_multi_configs().into_iter().map(|c| c.id).collect();
        let expected: Vec<&str> =
            Registry::builtin_ids().iter().chain(Registry::cross_builtin_ids()).copied().collect();
        assert_eq!(ids, expected);
    }
}
