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
| `ref_segments` | 7 | leading segments that form the reference when no stored baseline exists |
| `max_lag` | 6 | lags searched, in grid steps |
| `lag_margin` | 0.1 | a moved lag must correlate at least this much better than the reference lag in the same segment (added 2026-09-26) |
| `grid` | auto | alignment grid (spec 008) |

Evidence (correlation finding): `group_id`, `group_name`, `members`, `partner`, `rho`,
`rho_ref`, `delta`, `n_segments`, `n_points`.
Evidence (lag finding): `group_id`, `group_name`, `members`, `partner`, `lag_steps`,
`lag_ref_steps`, `grid_ns`, `n_segments`.
Metrics per pair and segment: `rho`, `lag_steps`.

## Behaviour

1. Align the pair (spec 008). Split into segments of `segment` length; segments with fewer
   than `min_points` complete bins are skipped.
2. Spearman ρ per segment (average ranks for ties). A segment never takes part in its own
   reference. Reference ρ_ref = the stored group profile's value when one exists (sprint 10);
   otherwise the median ρ of the first `ref_segments` usable segments of the window, and only
   the segments after them are judged. With fewer than 4 usable reference segments, or no
   segment after them, the pair is reported in `skipped` with `insufficient baseline` and
   emits metrics only. (The first `ref_segments` of a window are therefore assumed healthy;
   a break that starts at the very beginning of a window is found once a stored baseline
   exists.)
3. When \|ρ_ref\| < `min_ref`, the pair emits only metrics (it is not a related pair in this
   window).
4. A segment is broken when \|ρ − ρ_ref\| > `delta` or sign(ρ) ≠ sign(ρ_ref) with \|ρ\| > 0.2.
   Consecutive broken segments form one episode (ADR-0011) and one finding; the finding's
   window spans the episode and `rho` is the episode's worst segment value.
5. Lag: per segment the lag in `[-max_lag, max_lag]` maximising the cross-correlation;
   reference lag = median. Segments whose lag differs from the reference by more than one
   step, and whose best lag correlates at least `lag_margin` better than the reference lag in
   that segment, form lag episodes and lag findings (separate evidence shape, so they
   deduplicate separately).
6. Findings attach to the group's first member unless the pair is from a `redundant` group
   with a suspect named by spec 010 in the same run (not coupled in this spec: first member).
7. Summaries are plain language, e.g. "PT-101 stopped tracking TT-101 for 2 days
   (ρ 0.12, usually 0.91)".

## Acceptance criteria

- [x] Synthetic pair correlated at ρ≈0.9 with one decoupled day → exactly one finding over
      that day.
- [x] Sign flip over two days → one finding with negative `rho`.
- [x] A 3-step lag appearing for a day → one lag finding, no correlation finding.
- [x] Independent pair → no findings, metrics only.
- [x] Too little data → `skipped` with the reason, no findings.
- [x] Measured on a Petrobras 3W instance (P-PDG vs T-PDG): result and interpretation
      recorded in this spec (not a CI test; the dataset is not in the repository). See
      "Measurement".
- [x] Catalogue row 22 marked ✅ with the final params.

## Test cases

Unit (`checks::correlation_break::tests`): `decoupled_day`, `sign_flip`, `lag_shift`,
`negatively_related_pair_lag`, `independent_pair_is_silent`, `too_few_segments_skips`,
`ties_rank_correctly`, `every_pair_of_a_triple_is_judged_and_named`,
`bad_group_params_are_invalid_params`, `registry_runs_it_on_redundant_groups`. API:
`test_pair_findings_merge_only_with_the_same_partner`.

## Measurement (Petrobras 3W, 2026-09-23)

3W instances last hours, not weeks, so the run used `segment` 30m, `grid` 1m (mean per
minute of the 1 Hz data), `min_points` 24 and `ref_segments` 7 (a 3.5 h reference), via
`tabayyun check-multi <file> --ts-col timestamp --value-cols P-PDG,T-PDG`. The 3W files are
brotli-compressed Parquet, which the CLI's reader does not decode; they were rewritten with
zstd first.

| Instance | Class | Result |
|---|---|---|
| WELL-00001, three instances | 0, 1, 7 | P-PDG and T-PDG are constant 0 (dead gauges): no usable segment, `insufficient baseline`; the flatline check covers these |
| WELL-00015_20170620122925 | 5 (rapid productivity loss) | 14 segments, ρ_ref 0.22 < `min_ref`: not a related pair here, metrics only |
| WELL-00019_20141117190526 | 8 (hydrate in production line), 3.6 days | 175 segments, ρ_ref −0.94, lag 0 throughout; one finding, 2014-11-17 22:30–23:00, ρ −0.40 |

Interpretation: on WELL-00019 the downhole pressure and temperature move in lockstep with
ρ ≈ −1 for three days, including the labelled hydrate transient (class 108 from
2014-11-18 07:27) and steady state; a hydrate downstream of the wellhead does not decouple the
downhole pair, so this check is not the detector for class 8. The one finding lies in the
labelled normal period: ρ really weakened for half an hour, but no labelled event explains it.
One of the seven reference segments already had ρ −0.48, so at 30-minute segments normal
operation occasionally dips this far; a stored baseline (sprint 10) with a spread-based
threshold would judge it more tightly than a fixed `delta`.

The first measurement also found two lag bugs, fixed before this record: the lag search
maximised the signed correlation, which for a negatively related pair picks the least negative
lag (random lags, four false lag findings on WELL-00019), and raw levels of slowly trending
series correlate at every lag, which flattens the profile. See "Implementation edits".

## Live check (2026-09-23, after PR #33, release run 33)

Two hourly series over 12 days (`live-009-a`, `live-009-b`: a shared AR(1) driver plus a daily
sine; b decoupled to noise on day 10), a `related` group and a fixed 12-day dataset gave a
dataset run on the CapRover worker with exactly one `tby.correlation_break` finding on
`live-009-a`, window 2024-02-11 (day 10), "live-009-a stopped tracking live-009-b for 1d
(ρ 0.13, usually 1.00)", `partner` = live-009-b's id. The other three findings were
single-series checks on the synthetic data; all four were resolved as "test upload".

## Implementation edits

Recorded on 2026-09-23; approved with the plan.

- A finding attaches to the pair member that comes first in the group, not the group's first
  member (which need not be in the pair); `partner` names the other. Metrics are named
  `rho:<partner>` and `lag_steps:<partner>`, because metric points are keyed by
  (series, check, name, ts) and the pairs of one series would overwrite each other.
- Dedup: a finding with a `partner` merges only into a finding with the same `partner`
  (second ADR-0013 amendment), so the pairs (a, b) and (a, c) of one group stay apart.
- Lag is read from first differences, maximising the correlation times the segment's sign of
  ρ. It is judged only in segments whose correlation held and whose best sign-matched
  cross-correlation is at least `min_ref`, and only when the reference lags are stable (MAD
  ≤ 1 step); otherwise a decoupled day would also report a random lag, and a pair without a
  sharp cross-correlation peak would report noise.
- Corrected 2026-09-26: `lag_margin` (default 0.1). The stability test above let through
  pairs whose reference lag sat at ±1 step while daily peaks wandered to ±2: on three
  redundant 15-minute meters with a smooth daily curve the example seed got 8–12 "lags by
  30m" findings. In every such segment the "moved" lag fit at most 0.03 better than the
  reference lag; the planted 3-step shifts fit about 1.0 better. Test:
  `smooth_noisy_pair_has_no_lag_findings`.
- Group `params` override the check's params key by key; keys the check does not know (other
  checks' params) are ignored, a wrong type is `InvalidParams`, and so are a zero `segment` or
  `grid` and a `max_lag` above 240 steps.
- The `insufficient baseline` skip names the pair and the number of usable segments.
  Segments are UTC-aligned (`ts / segment`), so a skipped day separates episodes.

## Out of scope

- Auto-suggesting related pairs (sprint 10, S10-2).
- Nonlinear dependence measures (mutual information): R2.
