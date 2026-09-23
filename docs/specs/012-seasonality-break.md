# Spec 012 — Seasonality in the profile, `tby.seasonality_break`

Sprint 7, story S7-7. Depends on: nothing new (single-series). Packages:
`core/tabayyun-core/src/{profile.rs,seasonal.rs}`,
`core/tabayyun-core/src/checks/seasonality_break.rs`, `docs/checks/catalogue.md`.

## Goal

The baseline profile knows whether a series has a daily, weekly or yearly rhythm and how
strong it is. `tby.seasonality_break` reports the periods where a strongly periodic series
loses its rhythm or switches to a different period (a load profile that stops showing its
daily shape, a PV feed-in that turns flat). Catalogue check 21. The profile fields also
serve later work: the seasonal spike variant, `impute.seasonal` (S14-5), fleet baselines.

## User story

As an energy analyst, I learn that a feeder's daily load curve disappeared last week, which
means the meter was replaced by a constant estimate.

## Interface

`Profile` gains:

| Field | Type | Meaning |
|---|---|---|
| `dominant_period_ns` | `Option<i64>` | the candidate period with the highest autocorrelation, if any is significant |
| `seasonal_strength` | `Option<f64>` | 0–1, Hyndman's F_S for that period |

`seasonal.rs` (pure functions, reused by the check):

```rust
pub fn regularise(frame: &SeriesFrame, step_ns: i64) -> (i64, Vec<f64>);   // mean per bin, NaN gaps
pub fn acf_at(values: &[f64], lag: usize) -> Option<f64>;                  // pairwise-complete
pub fn seasonal_strength(values: &[f64], period: usize) -> Option<f64>;
pub fn detect(frame: &SeriesFrame, candidates_ns: &[i64]) -> Option<(i64, f64)>; // (period, strength)
```

Check params:

| Param | Default | Meaning |
|---|---|---|
| `min_strength` | 0.6 | a reference weaker than this is not seasonal; the check is silent |
| `drop` | 0.5 | flag when strength < (1 − drop) × reference |
| `candidates` | `["1d", "7d", "365d"]` | periods considered |
| `segment` | auto | max(4 × period, 7d) |

Evidence: `period_ns`, `period_ref_ns`, `strength`, `strength_ref`, `reason`
(`weaker` or `period_changed`), `n_segments`. Metrics per segment: `seasonal_strength`.

## Behaviour

1. Candidates are used only when period ≥ 4 × expected interval and the data spans at least
   3 periods; the series is regularised to its expected interval (or to 1 h when finer than
   that, to bound cost) before the ACF.
2. `detect`: the candidate with the highest ACF at its lag, if that ACF ≥ 0.3; strength via
   classical decomposition on the regularised series: trend = centred moving average of
   length `period`, seasonal = mean detrended value per phase, remainder R;
   F_S = max(0, 1 − Var(R) / Var(S + R)). NaN bins are skipped in every mean and variance.
3. The check computes strength and period per segment. Reference = the run's baseline
   profile when present (`ctx.profile`), else the median of the segment strengths and the
   modal segment period (with fewer than 4 segments the check skips with `too few periods`).
4. When the reference strength is below `min_strength`, no findings (metrics only).
5. A segment is broken when its strength < (1 − `drop`) × reference, or its dominant period
   differs from the reference while its own strength ≥ `min_strength` (`period_changed`).
   Consecutive broken segments form one episode and one finding (ADR-0011).
6. Summary example: "Daily pattern weakened for 9 days (strength 0.18, usually 0.87)".

## Acceptance criteria

- [ ] `detect` finds 1 d on a synthetic sinusoid with noise (strength > 0.8) and none on
      white noise and on a random walk.
- [ ] Synthetic daily series with one flat week → one finding over that week.
- [ ] A switch from daily to weekly rhythm → `period_changed` finding.
- [ ] Non-seasonal series → no findings, metrics only.
- [ ] Measured on OPSD DE load (strong daily/weekly, expected: no findings over normal
      weeks) and OPSD wind (not seasonal, expected: silent); results recorded here.
- [ ] Profile JSON includes the two new fields; existing profile consumers unaffected.
- [ ] Catalogue row 21 marked ✅.

## Test cases

Unit (`seasonal::tests`): `detects_daily`, `ignores_noise`, `ignores_random_walk`,
`strength_bounds`, `nan_bins_skipped`.
Unit (`checks::seasonality_break::tests`): `flat_week_flagged`, `period_change_flagged`,
`not_seasonal_is_silent`, `too_few_periods_skips`, `uses_baseline_when_present`.

## Out of scope

- MSTL with several periods at once and STL robustness weights (R2; the catalogue's
  "augurs" note is replaced by this pure-Rust method, ADR-0015).
- Seasonal spike variant (S-H-ESD) in `tby.spikes`: later, once this profile field exists.
