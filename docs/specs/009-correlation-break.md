# Spec 009 — `tby.correlation_break`: related series stopped agreeing

Sprint 7, story S7-4. Depends on: 008. Packages:
`core/tabayyun-core/src/checks/correlation_break.rs`, `docs/checks/catalogue.md`.

## Goal

For every pair in a `related` or `redundant` group, the check learns how strongly the two
series usually move together and reports the periods where that relationship breaks (the
rank correlation falls away or flips sign) or where one series starts lagging the other.
Catalogue check 22.

## User story

As a reliability engineer, I learn that the downhole pressure stopped tracking the
temperature on Tuesday, before anyone trusts a model fed by one of them.

## Interface

`CrossCheck` for kinds `related` and `redundant`; `Dimension::Consistency`, default
severity high. Params (group `params` override check params):

| Param | Default | Meaning |
|---|---|---|
| `segment` | `1d` | length of the rolling window ρ is computed over |
| `delta` | 0.3 | flag when \|ρ − ρ_ref\| exceeds this |
| `min_ref` | 0.5 | below this \|ρ_ref\| the pair is not related enough to judge |
| `min_points` | 24 | aligned points a segment needs |
| `max_lag` | 6 | lags searched, in grid steps |
| `grid` | auto | alignment grid (spec 008) |

Evidence (correlation finding): `group_id`, `group_name`, `members`, `partner`, `rho`,
`rho_ref`, `delta`, `n_segments`, `n_points`.
Evidence (lag finding): `group_id`, `group_name`, `members`, `partner`, `lag_steps`,
`lag_ref_steps`, `grid_ns`, `n_segments`.
Metrics per pair and segment: `rho`, `lag_steps`.

## Behaviour

1. Align the pair (spec 008). Split into segments of `segment` length; segments with fewer
   than `min_points` complete bins are skipped.
2. Spearman ρ per segment (average ranks for ties). Reference ρ_ref = median of the segment
   ρs, or the baseline value when a stored group profile exists (not yet: sprint 10). With
   fewer than 4 usable segments there is no reference and the pair is reported in
   `skipped` with `too few segments`.
3. When \|ρ_ref\| < `min_ref`, the pair emits only metrics (it is not a related pair in this
   window).
4. A segment is broken when \|ρ − ρ_ref\| > `delta` or sign(ρ) ≠ sign(ρ_ref) with \|ρ\| > 0.2.
   Consecutive broken segments form one episode (ADR-0011) and one finding; the finding's
   window spans the episode and `rho` is the episode's worst segment value.
5. Lag: per segment the lag in `[-max_lag, max_lag]` maximising the cross-correlation;
   reference lag = median. Segments whose lag differs from the reference by more than one
   step form lag episodes and lag findings (separate evidence shape, so they deduplicate
   separately).
6. Findings attach to the group's first member unless the pair is from a `redundant` group
   with a suspect named by spec 010 in the same run (not coupled in this spec: first member).
7. Summaries are plain language, e.g. "PT-101 stopped tracking TT-101 for 2 days
   (ρ 0.12, usually 0.91)".

## Acceptance criteria

- [ ] Synthetic pair correlated at ρ≈0.9 with one decoupled day → exactly one finding over
      that day.
- [ ] Sign flip over two days → one finding with negative `rho`.
- [ ] A 3-step lag appearing for a day → one lag finding, no correlation finding.
- [ ] Independent pair → no findings, metrics only.
- [ ] Too little data → `skipped` with the reason, no findings.
- [ ] Measured on a Petrobras 3W instance (P-PDG vs T-PDG): result and interpretation
      recorded in this spec (not a CI test; the dataset is not in the repository).
- [ ] Catalogue row 22 marked ✅ with the final params.

## Test cases

Unit (`checks::correlation_break::tests`): `decoupled_day`, `sign_flip`, `lag_shift`,
`independent_pair_is_silent`, `too_few_segments_skips`, `ties_rank_correctly`.

## Out of scope

- Auto-suggesting related pairs (sprint 10, S10-2).
- Nonlinear dependence measures (mutual information): R2.
