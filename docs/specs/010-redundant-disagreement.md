# Spec 010 — `tby.redundant_disagreement`: redundant sensors disagree

Sprint 7, story S7-5. Depends on: 008. Packages:
`core/tabayyun-core/src/checks/redundant_disagreement.rs`, `docs/checks/catalogue.md`.

## Goal

Sensors that measure the same quantity (duplex transmitters, 2oo3 voting sets, main and
check meters) are compared continuously. When they differ by more than a tolerance for long
enough, a finding reports the disagreement, and with three or more sensors it names the one
that is off. Catalogue check 23.

## User story

As an instrument technician, I see that PT-101B drifted 4 % away from its two partners
three days ago, so I calibrate B instead of all three.

## Interface

`CrossCheck` for kind `redundant`; `Dimension::Accuracy`, default severity high. Params:

| Param | Default | Meaning |
|---|---|---|
| `tolerance` | — | absolute allowed difference (engineering units) |
| `tolerance_pct` | — | allowed difference as % of the members' median magnitude |
| `k` | 4.0 | auto tolerance = max(k × 1.4826 × MAD(difference), 2 × resolution) |
| `min_duration` | `15m` | a disagreement must last this long |
| `grid` | auto | alignment grid (spec 008) |

Precedence: `tolerance`, else `tolerance_pct`, else auto; `tolerance_source` in evidence
says which (`param`, `pct`, `auto`).

Evidence: `group_id`, `group_name`, `members`, `suspect` (series id or null), `tolerance`,
`tolerance_source`, `max_abs_diff`, `mean_abs_diff`, `duration_ns`, `n_points`.
Metric per member and bin grid summary: `max_abs_diff` per segment of one day.

## Behaviour

1. Align members; ignore bins where fewer than two members have values.
2. Two sensors: difference d = A − B. Auto tolerance is centred at zero, so a constant bias
   larger than the noise is reported (a bias *is* a disagreement); if two sensors are
   meant to differ, the group sets `tolerance`.
3. Three or more: per bin, the median of members; a member deviates when |x − median| >
   tolerance. With exactly one deviating member in a bin, that member is the suspect for
   the bin.
4. Runs of disagreeing bins shorter than `min_duration` are ignored; runs separated by less
   than `min_duration` merge (episodes).
5. One finding per episode, attached to the suspect when one member was the suspect in at
   least 80 % of the episode's bins, otherwise to the first member with `suspect = null`
   ("cannot tell which of A and B is off").
6. Summary example: "PT-101B reads up to 4.2 bar away from PT-101A/C for 3 days (tolerance
   1.0 bar)".

## Acceptance criteria

- [x] 2oo3 synthetic: one member drifts past tolerance for 6 h → one finding attached to
      that member, `suspect` set.
- [x] Two members with a 30-minute disagreement → one finding, `suspect` null.
- [x] A 10-minute blip → no finding (`min_duration`).
- [x] Explicit `tolerance` suppresses a designed offset; `tolerance_pct` works on scaled data.
- [x] Bins with a single member present are ignored.
- [x] Catalogue row 23 marked ✅.

## Test cases

Unit (`checks::redundant_disagreement::tests`): `two_of_three_names_suspect`,
`pair_without_suspect`, `short_blip_ignored`, `explicit_tolerance`, `pct_tolerance`,
`auto_tolerance_catches_bias`, `sparse_bins_ignored`, `bad_params_are_invalid`,
`registry_runs_it_on_redundant_groups_only`, `numbers_read_well`.

## Live check (2026-09-23, after PR #34, release run 34)

Three hourly series over 7 days (`live-010-a`, `-b`, `-c`: one shared AR(1) signal plus a daily
sine and 0.05 bar noise each; `-c` reads 5 bar high on 2024-02-05 from 08:00 to 14:00 UTC), a
`redundant` group with params `{"tolerance": 1.0}` and a fixed 7-day dataset gave a dataset run
on the CapRover worker with exactly one `tby.redundant_disagreement` finding, on `live-010-c`,
window 2024-02-05 08:00–14:00, "live-010-c reads up to 5.02 bar away from live-010-a/live-010-b
for 6h (tolerance 1 bar)", `suspect` = live-010-c's id, `tolerance_source` = `param`. The other
three findings were `tby.level_drift` on the synthetic random walk; all four were resolved as
"test upload".

## Implementation edits

Recorded on 2026-09-23; approved with the plan.

- The metric is `max_abs_diff:<group_id>` rather than `max_abs_diff`: metric points are keyed by
  (series, check, name, ts), and a series in two redundant groups would overwrite its own line.
- With three or more members the automatic tolerance uses the MAD of every member's deviation
  from the bin median (the spec gave the formula for a pair's difference only).
  - Corrected 2026-09-26: in a bin with an odd number of values one member *is* the median, and
    its deviation is 0 by construction. Pooling those zeros roughly halved the tolerance for three
    members, and three healthy meters with 15-minute data gave 110–166 findings each on
    staging. The median member's 0 is now left out (one per odd bin). Test:
    `auto_tolerance_three_members_ignores_the_median_members_zero`.
- Without a `resolution` in the metadata the floor uses the resolution estimated from the data
  (`profile::resolution`), so identical quantised readings (MAD 0) never give a zero tolerance.
- Step 4 runs in the order written: runs shorter than `min_duration` are dropped first, then the
  rest merge when closer than `min_duration`, so two 10-minute blips 5 minutes apart stay silent.
- A bin where only two of three or more members are present still counts (compared with their
  mean), but names no suspect. `tolerance` wins over `tolerance_pct` when both are set.
- `max_abs_diff` and `mean_abs_diff` measure the suspect's distance from the median when there is
  one, else the largest member's; for a pair, |A − B|.
- Group `params` override the check's through the shared `cross::with_group_params` (moved there
  from spec 009's check); a negative `tolerance` or `tolerance_pct`, a non-positive `k` and a zero
  `grid` are `InvalidParams`.

## Out of scope

- SIS discrepancy override and growing-deviation signature (S17-1).
- Meter class accuracy tables (S14-1 presets).
