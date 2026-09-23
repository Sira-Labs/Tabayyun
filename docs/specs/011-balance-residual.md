# Spec 011 — `tby.balance_residual`: energy or mass balance violated

Sprint 7, story S7-6. Depends on: 008. Packages:
`core/tabayyun-core/src/checks/balance_residual.rs`, `docs/checks/catalogue.md`.

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
| `k` | 3.0 | flag when \|r\| > k × σ_r |
| `uncertainty` | 0.01 | relative standard uncertainty per member (fraction); a map `{series_id: u}` overrides per member |
| `loss_min` | -0.01 | lower bound of the loss share r / Σin |
| `loss_max` | 0.05 | upper bound of the loss share |
| `min_duration` | `1h` | a violation must last this long |
| `grid` | auto | alignment grid (spec 008) |

σ_r per bin = sqrt(Σ (u_i · x_i)²). Loss share = r / Σin (bins with Σin ≤ 0 are ignored for
the share).

Evidence: `group_id`, `group_name`, `members`, `inputs`, `outputs`, `suspect`,
`residual_mean`, `z_max`, `loss_share_mean`, `loss_min`, `loss_max`, `contributions`, `reason`
(`sigma`, `loss_band` or `both`), `duration_ns`, `n_points`.
Metrics per bin summary (per day): `residual_mean`, `loss_share_mean`.

## Behaviour

1. Align members; a bin counts only when every member has a value.
2. Flag a bin when \|r\| > k × σ_r or the loss share is outside `[loss_min, loss_max]`.
3. Runs shorter than `min_duration` are ignored; nearby runs merge into episodes.
4. Suspect: a single balance equation cannot isolate a gross error statistically (every
   member's measurement test equals \|r\| / σ_r), so the suspect comes from change over
   time, as an exact decomposition of the residual share:
   - per bin, throughput T = (Σin + Σout) / 2, member share q_i = x_i / T, sign s_i = +1 for
     inputs and −1 for outputs, residual share ρ = r / T = Σ s_i q_i;
   - baseline B = the unflagged complete bins of the window with T > 0, episode E = the
     episode's complete bins with T > 0 (a zero-throughput bin has no shares); means over
     bins are plain arithmetic means;
   - Δρ = mean_E(ρ) − mean_B(ρ), and member contributions c_i = s_i (mean_E(q_i) − mean_B(q_i)),
     which sum exactly to Δρ;
   - the suspect is the member with the largest c_i / Δρ, if that ratio is ≥ 0.8;
   - `suspect = null` when B or E is empty, when \|Δρ\| < 1e-9, or when no ratio reaches 0.8.
   The finding attaches to the suspect, or to the first input when there is none; evidence
   adds `contributions` (`{series_id: c_i / Δρ}`, rounded to 3 digits).
5. Summary example: "Balance SS-North does not close for 5 h: outputs exceed inputs by
   3.1 % (band −1 % to 5 %), feeder F3 explains most of it".

## Acceptance criteria

- [ ] Synthetic inlet/outlet with a 3 % loss inside the band → no finding.
- [ ] An outlet meter scaled by 0.9 for 6 h → one finding, `suspect` = that meter.
- [ ] Loss share above `loss_max` with small σ → finding with `reason` `loss_band`.
- [ ] Two members drifting together → finding with `suspect` null.
- [ ] Bins with a missing member are ignored; per-member `uncertainty` map honoured.
- [ ] Catalogue row 24 marked ✅.

## Test cases

Unit (`checks::balance_residual::tests`): `closing_balance_is_silent`,
`scaled_outlet_named`, `loss_band_violation`, `joint_drift_no_suspect`,
`missing_member_bins_ignored`, `per_member_uncertainty`.

## Out of scope

- VDI 2048 data reconciliation with covariance and the `reconcile.balance` repair (S17-2).
- Multi-node networks with shared members (R2 grid pack).
