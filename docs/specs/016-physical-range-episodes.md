# Spec 016 — `tby.physical_range` reports episodes, not every excursion

Sprint 7, story S7-9 (C). Depends on: nothing new. Packages:
`core/tabayyun-core/src/checks/physical_range.rs`, `docs/checks/catalogue.md`.

## Goal

A limit breached many times in a short span is one problem, not dozens. Excursions closer
than a gap become one finding with an exact count, and a series with more episodes than a cap
gets one summary finding, as `tby.spikes` already does under ADR-0011. Found in spec 005's
local check: a limit set inside the normal range produced 34 findings.

## User story

As an operator, one finding tells me the level ran above its physical maximum 34 times this
afternoon, instead of 34 findings I have to triage one by one.

## Interface

New params (existing `physical_min/max`, `capacity_factor_max` unchanged):

| Param | Default | Meaning |
|---|---|---|
| `cluster_gap` | auto = max(1 h, 12 × interval) | excursions closer than this merge |
| `max_findings` | 20 | above this, one summary finding |

Evidence per episode keeps today's keys (`count`, `min_observed`, `max_observed`,
`limit_min`, `limit_max`) and adds `n_excursions`, `first_ts`, `last_ts`. The summary finding
has `n_episodes`, `count`, `share`, `min_observed`, `max_observed`, `limit_min`, `limit_max`,
`largest` (up to 5 episodes with window and count); severity low (ADR-0011 summary rule).

## Behaviour

1. Excursion runs are found as today.
2. Runs whose gap to the previous run is below `cluster_gap` join its episode; one finding per
   episode, window from the first excursion start to the last excursion end, severity
   critical, `count` = samples outside the limits.
3. With more than `max_findings` episodes, the episodes are replaced by one summary finding
   over the whole window.
4. Because the evidence key set changes, stored findings of the old shape do not merge with
   new ones (ADR-0013); the spec's PR notes this for the live data.

## Acceptance criteria

- [ ] 34 excursions within two hours → one finding with `n_excursions = 34`.
- [ ] Two excursions a day apart → two findings.
- [ ] 50 separated episodes → one low-severity summary finding.
- [ ] Existing physical_range tests updated and passing; catalogue entry updated.

## Test cases

Unit (`checks::physical_range::tests`): `clustered_excursions_one_finding`,
`distant_excursions_separate`, `many_episodes_summary`, existing tests.

## Out of scope

- The same treatment for `tby.operational_range` and `tby.non_negative`: follow-up after
  this lands if their live output is noisy.
