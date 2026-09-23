//! Series groups and cross-series checks (spec 008).
//!
//! A [`SeriesGroup`] names series that belong together and how: `related` (they usually move
//! together), `redundant` (they measure the same quantity) or `balance` (inputs minus outputs
//! should close within a loss band). A [`CrossCheck`] runs on the frames of one group; the
//! registry runs every cross check whose [`CrossCheck::kinds`] include the group's kind.
//!
//! Cross-series findings are attached to one member series (the suspect when the check can
//! tell, otherwise the group's first member) and always carry `group_id`, `group_name` and
//! `members` in their evidence, so the stored finding shape stays stable (ADR-0013).

use crate::checks::{CheckContext, CheckOutput};
use crate::error::{Error, Result};
use crate::finding::{Dimension, Severity};
use crate::frame::SeriesFrame;
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use std::collections::HashSet;

/// Smallest and largest number of members a group may have.
pub const MIN_MEMBERS: usize = 2;
pub const MAX_MEMBERS: usize = 32;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum GroupKind {
    Related,
    Redundant,
    Balance,
}

impl GroupKind {
    pub fn as_str(self) -> &'static str {
        match self {
            GroupKind::Related => "related",
            GroupKind::Redundant => "redundant",
            GroupKind::Balance => "balance",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum MemberRole {
    #[default]
    Member,
    Input,
    Output,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct GroupMember {
    pub series_id: String,
    #[serde(default)]
    pub role: MemberRole,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SeriesGroup {
    pub id: String,
    pub name: String,
    pub kind: GroupKind,
    pub members: Vec<GroupMember>,
    /// Check parameters for this group (e.g. a balance's loss band); an object or null.
    #[serde(default)]
    pub params: serde_json::Value,
}

impl SeriesGroup {
    /// Member series ids in declaration order.
    pub fn member_ids(&self) -> impl Iterator<Item = &str> {
        self.members.iter().map(|m| m.series_id.as_str())
    }

    /// Structural rules shared by the API, the bindings and the CLI: 2–32 distinct members;
    /// `related` and `redundant` members all have role `member`; a `balance` has at least one
    /// input and one output and no plain member; `params` is an object or null.
    pub fn validate(&self) -> Result<()> {
        let bad = |reason: String| Err(Error::InvalidGroup { group: self.id.clone(), reason });
        let n = self.members.len();
        if !(MIN_MEMBERS..=MAX_MEMBERS).contains(&n) {
            return bad(format!("needs {MIN_MEMBERS} to {MAX_MEMBERS} members, has {n}"));
        }
        let mut seen = HashSet::new();
        if let Some(dup) = self.members.iter().find(|m| !seen.insert(m.series_id.as_str())) {
            return bad(format!("series {} is listed twice", dup.series_id));
        }
        let count = |role: MemberRole| self.members.iter().filter(|m| m.role == role).count();
        match self.kind {
            GroupKind::Related | GroupKind::Redundant if count(MemberRole::Member) != n => {
                return bad(format!("{} groups take role `member` only", self.kind.as_str()));
            }
            GroupKind::Balance if count(MemberRole::Member) > 0 => {
                return bad("balance groups take roles `input` and `output` only".into());
            }
            GroupKind::Balance if count(MemberRole::Input) == 0 || count(MemberRole::Output) == 0 => {
                return bad("balance groups need at least one input and one output".into());
            }
            _ => {}
        }
        if !(self.params.is_null() || self.params.is_object()) {
            return bad("params must be a JSON object".into());
        }
        Ok(())
    }

    /// The evidence keys every cross-series finding carries (spec 008).
    pub fn evidence_base(&self) -> serde_json::Map<String, serde_json::Value> {
        let mut m = serde_json::Map::new();
        m.insert("group_id".into(), self.id.clone().into());
        m.insert("group_name".into(), self.name.clone().into());
        m.insert("members".into(), self.member_ids().collect::<Vec<_>>().into());
        m
    }
}

/// A cross check's params with the group's `params` on top, key by key. Keys the check does
/// not know (another check's params) are ignored; a value of the wrong type is
/// `InvalidParams` naming the group.
pub fn with_group_params<T: Serialize + DeserializeOwned>(
    check: &T,
    id: &str,
    group: &SeriesGroup,
) -> Result<T> {
    let mut merged = serde_json::to_value(check)?;
    if let (Some(base), Some(over)) = (merged.as_object_mut(), group.params.as_object()) {
        for (k, v) in over {
            if base.contains_key(k) {
                base.insert(k.clone(), v.clone());
            }
        }
    }
    serde_json::from_value(merged)
        .map_err(|e| Error::InvalidParams { check: id.into(), reason: format!("group {}: {e}", group.id) })
}

/// A group the registry did not run, and why (feeds `stats.groups_skipped`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct GroupSkip {
    pub group_id: String,
    pub reason: String,
    /// Member series without data in the window.
    pub missing: Vec<String>,
}

/// A check over the aligned members of one group. Like [`crate::Check`] it is pure.
pub trait CrossCheck: Send + Sync {
    fn id(&self) -> &'static str;
    fn dimension(&self) -> Dimension;
    fn default_severity(&self) -> Severity;
    /// Group kinds this check applies to.
    fn kinds(&self) -> &'static [GroupKind];
    /// `frames` holds the group's members in declaration order, each with data.
    fn run(&self, frames: &[&SeriesFrame], group: &SeriesGroup, ctx: &CheckContext) -> Result<CheckOutput>;
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn group(kind: GroupKind, members: &[(&str, MemberRole)]) -> SeriesGroup {
        SeriesGroup {
            id: "g1".into(),
            name: "G1".into(),
            kind,
            members: members
                .iter()
                .map(|(id, role)| GroupMember { series_id: id.to_string(), role: *role })
                .collect(),
            params: serde_json::Value::Null,
        }
    }

    #[test]
    fn validates_member_count_duplicates_and_roles() {
        use MemberRole::*;
        assert!(group(GroupKind::Redundant, &[("a", Member), ("b", Member)]).validate().is_ok());
        assert!(group(GroupKind::Balance, &[("a", Input), ("b", Output)]).validate().is_ok());
        let err = |g: SeriesGroup| g.validate().unwrap_err().to_string();
        assert!(err(group(GroupKind::Related, &[("a", Member)])).contains("2 to 32"));
        assert!(err(group(GroupKind::Related, &[("a", Member), ("a", Member)])).contains("twice"));
        assert!(err(group(GroupKind::Redundant, &[("a", Member), ("b", Input)])).contains("role `member`"));
        assert!(err(group(GroupKind::Balance, &[("a", Input), ("b", Member)])).contains("input` and `output"));
        assert!(err(group(GroupKind::Balance, &[("a", Input), ("b", Input)])).contains("one output"));
        let many: Vec<(String, MemberRole)> = (0..33).map(|i| (format!("s{i}"), Member)).collect();
        let many: Vec<(&str, MemberRole)> = many.iter().map(|(s, r)| (s.as_str(), *r)).collect();
        assert!(err(group(GroupKind::Related, &many)).contains("has 33"));
        let mut g = group(GroupKind::Related, &[("a", Member), ("b", Member)]);
        g.params = json!([1]);
        assert!(err(g).contains("object"));
    }

    #[test]
    fn deserialises_with_default_role_and_params() {
        let g: SeriesGroup = serde_json::from_value(json!({
            "id": "g", "name": "PT-101", "kind": "redundant",
            "members": [{"series_id": "a"}, {"series_id": "b"}]
        }))
        .unwrap();
        assert_eq!(g.members[1].role, MemberRole::Member);
        assert!(g.params.is_null());
        assert_eq!(
            serde_json::Value::Object(g.evidence_base()),
            json!({"group_id": "g", "group_name": "PT-101", "members": ["a", "b"]})
        );
    }
}
