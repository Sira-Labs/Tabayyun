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

- [ ] 2oo3 synthetic: one member drifts past tolerance for 6 h → one finding attached to
      that member, `suspect` set.
- [ ] Two members with a 30-minute disagreement → one finding, `suspect` null.
- [ ] A 10-minute blip → no finding (`min_duration`).
- [ ] Explicit `tolerance` suppresses a designed offset; `tolerance_pct` works on scaled data.
- [ ] Bins with a single member present are ignored.
- [ ] Catalogue row 23 marked ✅.

## Test cases

Unit (`checks::redundant_disagreement::tests`): `two_of_three_names_suspect`,
`pair_without_suspect`, `short_blip_ignored`, `explicit_tolerance`, `pct_tolerance`,
`auto_tolerance_catches_bias`, `sparse_bins_ignored`.

## Out of scope

- SIS discrepancy override and growing-deviation signature (S17-1).
- Meter class accuracy tables (S14-1 presets).
