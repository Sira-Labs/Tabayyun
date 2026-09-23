# Spec 011 — `tby.balance_residual`: energy or mass balance violated

Sprint 7, story S7-6. Depends on: 008. Packages:
`core/tabayyun-core/src/checks/balance_residual.rs`, `core/tabayyun-core/src/cross.rs`, `docs/checks/catalogue.md`.

## Goal

For a balance group (inputs, outputs, an expected loss band), the residual Σin − Σout is
tracked against the measurement uncertainty of its members and against the loss band.
Periods where the balance does not close become findings, and the member whose change best
explains the residual is named when one stands out. Catalogue check 24.

## User story

As a network analyst, I see that the substation balance stopped closing on Monday and that
feeder F3's meter explains the gap, before the losses report goes out.

## Interface

`CrossCheck` for kind `balance`; `Dimension::Consistency`, default severity high. Params:

| Param | Default | Meaning |
|---|---|---|
| `k` | 3.0 | flag when r lies outside the loss band by more than k × σ_r |
| `uncertainty` | 0.01 | relative standard uncertainty per member (fraction); a map `{series_id: u}` overrides per member |
| `loss_min` | -0.01 | lower bound of the loss share r / Σin |
| `loss_max` | 0.05 | upper bound of the loss share |
| `min_duration` | `1h` | a violation must last this long |
| `grid` | auto | alignment grid (spec 008) |

σ_r per bin = sqrt(Σ σ_i²) with σ_i = max(u_i · |x_i|, resolution_i). Loss share = r / Σin (bins
with Σin ≤ 0 have no share; their band collapses to r = 0).

Evidence: `group_id`, `group_name`, `members`, `inputs`, `outputs`, `suspect`,
`residual_mean`, `z_max` (largest distance outside the band, in σ_r), `loss_share_mean`,
`loss_min`, `loss_max`, `contributions`, `reason` (`above_band` or `below_band`, by the majority
of the episode's bins), `duration_ns`, `n_points`.
Metrics per day, on the first input: `residual_mean:<group_id>`, `loss_share_mean:<group_id>`.

## Behaviour

1. Align members; a bin counts only when every member has a value.
2. Flag a bin when r lies outside the band [`loss_min` · Σin, `loss_max` · Σin] by more than
   k × σ_r. A loss inside the band is never flagged, and neither is a crossing smaller than
   the members' uncertainty.
3. Runs shorter than `min_duration` are ignored; nearby runs merge into episodes.
4. Suspect: a single balance equation cannot isolate a gross error statistically (every
   member's measurement test equals \|r\| / σ_r), so the suspect comes from change over
   time, as an exact decomposition of the residual share:
   - per bin, throughput T = (Σin + Σout) / 2, member share q_i = x_i / T, sign s_i = +1 for
     inputs and −1 for outputs, residual share ρ = r / T = Σ s_i q_i;
   - baseline B = the unflagged complete bins of the window with T > 0, episode E = the
     episode's complete bins with T > 0 (a zero-throughput bin has no shares); means over
     bins are plain arithmetic means;
   - scaling one member also moves T, so every share changes by a common factor; per member
     g_i = mean_E(q_i) / mean_B(q_i) − 1, and the common mode g_m = median(g_i) (the middle
     pair averaged for an even count);
   - member contributions c_i = s_i · mean_B(q_i) · (g_i − g_m), which sum to Δρ' = Δρ −
     g_m · mean_B(ρ), the change in residual share net of the common mode;
   - the suspect is the member with the largest c_i / Δρ', if that ratio is ≥ 0.8;
   - `suspect = null` (and `contributions` null) when B or E is empty, when a member's
     baseline share is 0, or when \|Δρ'\| < 1e-9; `suspect = null` when no ratio reaches 0.8.
     With one input and one output the ratios are always about 0.5 each: one balance of two
     meters cannot tell which is off.
   The finding attaches to the suspect, or to the first input when there is none; evidence
   adds `contributions` (`{series_id: c_i / Δρ'}`, rounded to 3 digits).
5. Summary example: "Balance SS-North does not close for 5h: outputs exceed inputs by
   3.1 % (band -1 % to 5 %); F3 explains most of it" (or "no single member explains it").

## Acceptance criteria

- [x] Synthetic inlet/outlet with a 3 % loss inside the band → no finding; a 4.5 % loss stays
      silent even with 0.1 % meters.
- [x] One of three outlet meters scaled for 6 h → one finding, `suspect` = that meter (0.7 with
      default parameters; 0.9 with 0.2 % meters and a 4 % band, see the edits), also when the
      load rises 30 % during the episode.
- [x] Loss share above `loss_max` with small σ → finding with `reason` `above_band`; a crossing
      within the meters' uncertainty → no finding.
- [x] Two members drifting together → finding with `suspect` null; one inlet and one outlet →
      `suspect` null.
- [x] Bins with a missing member are ignored; per-member `uncertainty` map honoured.
- [x] Catalogue row 24 marked ✅.

## Test cases

Unit (`checks::balance_residual::tests`): `closing_balance_is_silent`,
`loss_inside_band_is_silent_even_with_precise_meters`, `scaled_outlet_named`,
`small_scaling_needs_precise_meters_or_a_tight_band`, `load_change_keeps_suspect`,
`inlet_and_outlet_alone_name_no_suspect`, `joint_drift_no_suspect`, `loss_band_violation`,
`outputs_exceeding_inputs`, `missing_member_bins_ignored`, `per_member_uncertainty`,
`idle_members_floor_uncertainty_at_resolution`, `bad_params_are_invalid`,
`registry_runs_it_on_balance_groups_only`; `cross::tests::episodes_drop_short_runs_then_merge`.

## Implementation edits

Recorded on 2026-09-23; both rule changes were approved with the plan, after a simulation
(Behaviour steps 2 and 4 above are the edited text).

- **Flag rule.** The spec flagged |r| > k × σ_r *or* a share outside the band. The first test
  includes the expected loss in r, so a 4.5 % loss inside a 5 % band was 3.3 σ_r with 1 % meters
  and flagged; and "above the band with small σ" could never give `reason` `loss_band` alone.
  Now a bin is flagged when r is outside the band by more than k × σ_r, and `reason` names the
  side (`above_band`, `below_band`). The price is sensitivity: the band is widened by k × σ_r
  (about 3.4 % of throughput with 1 % meters), so a 10 % error on a feeder carrying 27 % of the
  flow (+2.7 % of loss share) is found only with the meters' real class (e.g. 0.2 %) or a band
  set to the network's losses. Class-accuracy presets are S14-1.
- **Suspect.** The spec's decomposition divided by the bin's throughput, which the faulty meter
  itself moves, so the opposite side took about half of every contribution: a scaled meter
  scored 0.76–0.81 against the 0.8 threshold depending on its size. Removing the common mode
  (median relative change of all shares) gives it 0.84–0.96 in the same cases, and the result
  does not change when the load rises during the episode. The median averages the middle pair:
  `profile::median_mad` takes the nearest rank, which pinned a two-member balance's common mode
  on one member and named it.
- σ_i is floored at the member's resolution (metadata, else estimated), so an idle line reading
  one count does not make every bin significant; with no input the band collapses to r = 0.
- The run/episode building, the summary number format and the member display name moved from
  spec 010's check to `cross.rs` and are shared (behaviour unchanged).
- Metrics carry the group (`residual_mean:<group_id>`, `loss_share_mean:<group_id>`), as in
  spec 010, and sit on the first input. An `uncertainty` map in a group's params naming a
  non-member, negative or non-finite uncertainties, `loss_min` ≥ `loss_max`, a non-positive `k`
  and a zero `grid` are `InvalidParams`. A check-level map serves every balance group, so its
  entries for other groups' series are ignored (an error there would stop the dataset run).

## Out of scope

- VDI 2048 data reconciliation with covariance and the `reconcile.balance` repair (S17-2).
- Multi-node networks with shared members (R2 grid pack).
