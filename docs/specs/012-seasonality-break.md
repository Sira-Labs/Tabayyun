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
| `dominant_period_ns` | `Option<i64>` | the shortest candidate period with a significant (detrended) autocorrelation |
| `seasonal_strength` | `Option<f64>` | 0–1, Hyndman's F_S for that period |

`seasonal.rs` (pure functions, reused by the check):

```rust
pub fn regularise(frame: &SeriesFrame, step_ns: i64) -> (i64, Vec<f64>);   // mean per bin, NaN gaps
pub fn acf_at(values: &[f64], lag: usize) -> Option<f64>;                  // pairwise-complete
pub fn seasonal_strength(values: &[f64], period: usize) -> Option<f64>;
pub fn detect(frame: &SeriesFrame, candidates_ns: &[i64]) -> Option<(i64, f64)>; // (period, strength)
// also: step_for, moving_average, detrend, detrended_acf, detect_values (on regularised values)
```

Check params:

| Param | Default | Meaning |
|---|---|---|
| `min_strength` | 0.6 | a reference weaker than this is not seasonal; the check is silent |
| `drop` | 0.5 | flag when strength < (1 − drop) × reference |
| `candidates` | `["1d", "7d", "365d"]` | periods considered |
| `segment` | auto | max(4 × period, 7d) |
| `ref_segments` | 4 | leading segments that form the reference when no baseline profile exists |

Evidence: `period_ns` (the period detected in the episode, null when none), `period_ref_ns`,
`strength` (mean strength at the reference period), `strength_ref`, `strength_new` (strength
of the new period), `reason` (`weaker` or `period_changed`), `n_segments`, `segment_ns`.
Metrics per segment: `seasonal_strength` at the reference period.

## Behaviour

1. Candidates are used only when period ≥ 4 × expected interval and the data spans at least
   3 periods; the series is regularised to its expected interval (or to 1 h when finer than
   that, to bound cost) before the ACF.
2. `detect`: the shortest candidate whose ACF at its lag, after removing the trend (series
   minus its centred moving average of length `period`), is ≥ 0.3; strength via classical
   decomposition on the regularised series: trend = centred moving average of length
   `period`, seasonal = mean detrended value per phase, remainder R;
   F_S = max(0, 1 − Var(R) / Var(S + R)); a stretch without variance has strength 0. NaN
   bins are skipped in every mean and variance.
3. The check cuts the regularised series into epoch-anchored segments (auto length
   max(4 × period, 7 d)); a segment counts when at least half of its bins hold a value. A
   segment never takes part in its own reference (same rule as spec 009): the reference
   period is the shortest candidate that at least half of the first `ref_segments`
   (default 4) segments detect, the reference strength their median strength at it, and
   only later segments are judged. The run's profile is not used as the reference (see the
   edits). Within a segment only candidates spanning at most a third of it are detected.
   With too few segments for a reference or none after them the check skips with
   `insufficient baseline`.
4. When the reference strength is below `min_strength`, no findings (metrics only).
5. A segment is broken when its dominant period differs from the reference while that
   period's strength ≥ `min_strength` (`period_changed`), else when its strength at the
   reference period < (1 − `drop`) × reference (`weaker`).
   Consecutive broken segments form one episode and one finding (ADR-0011).
6. Summary examples: "Daily pattern weakened for 7d (strength 0.18, usually 0.87)";
   "Weekly pattern turned daily for 28d (daily strength 0.98; weekly strength 0.99, usually
   1.00)".

## Acceptance criteria

- [x] `detect` finds 1 d on a synthetic sinusoid with noise (strength > 0.8) and none on
      white noise and on a random walk.
- [x] Synthetic daily series with one flat week → one finding over that week.
- [x] A switch of rhythm → `period_changed` finding: weekly → daily with the defaults;
      daily → weekly with `segment` 28d (with 7-day segments it is `weaker`, see the edits).
- [x] Non-seasonal series → no findings, metrics only.
- [x] Measured on OPSD DE load (strong daily/weekly, expected: no findings over normal
      weeks) and OPSD wind (not seasonal, expected: silent); results recorded here.
- [x] Profile JSON includes the two new fields; existing profile consumers unaffected.
- [x] Catalogue row 21 marked ✅.

## Test cases

Unit (`seasonal::tests`): `detects_daily`, `ignores_noise`, `ignores_random_walk`,
`weekly_only_rhythm_is_weekly`, `strength_bounds`, `nan_bins_skipped`,
`regularise_to_hourly_means`, `moving_average_even_period_is_centred`.
Unit (`checks::seasonality_break::tests`): `flat_week_flagged`, `period_change_flagged`,
`daily_to_weekly_needs_a_long_segment_to_name_the_new_period`, `not_seasonal_is_silent`,
`too_few_periods_skips`, `self_profile_does_not_mask_break` (replaces
`uses_baseline_when_present`), `sparse_segment_not_judged`, `bad_params_are_invalid`,
`registered_as_builtin`.

## Measurement (OPSD 60-minute data, release 2020-10-06)

Hourly German series from 2015-01-01 to 2020-09-30 (about 50 300 samples each), default
parameters, `tabayyun run` with only this check (about 0.04 s per series):

| Series | Profile | Findings | Weekly strength min / median / max |
|---|---|---|---|
| `DE_load_actual_entsoe_transparency` | daily, 0.88 | 0 | 0.78 / 0.92 / 0.94 |
| `DE_wind_generation_actual` | not seasonal | 0 (metrics only) | 0.02 / 0.26 / 0.90 |
| `DE_solar_generation_actual` | daily, 0.74 | 0 | 0.65 / 0.94 / 1.00 |
| DE load, week 2019-03-07 to 03-14 replaced by its mean | daily | 1 | exactly that week, "Daily pattern weakened for 7d (strength 0.00, usually 0.92)" |

The weakest real load weeks (Easter 2016 at 0.78, then 0.80 and 0.81) stay far above the
flag level of 0.46; Christmas weeks do not trip it. Solar's winter weeks are its weakest
(0.65). Wind has no rhythm: its reference segments agree on no candidate, so it only reports
metrics.

## Implementation edits

Recorded on 2026-09-23; the first three were approved with the plan, after a simulation.

- **Detrended ACF.** The raw ACF at the candidate lag is high for anything that drifts: 0.93
  at one day for a random walk and 0.55 for a wind-like AR(0.98) process, both above 0.3, so
  both would have read as daily. The ACF is taken after removing the moving-average trend
  (random walk −0.04, wind −0.09, daily sine unchanged at 0.93).
- **Shortest significant candidate.** A daily rhythm correlates as well at 7 d as at 1 d (0.93
  both), so "the highest ACF" picked among harmonics by noise. The shortest candidate with a
  significant rhythm is taken; a load curve is daily (strength 0.95), which also keeps
  segments at 7 d instead of 28 d.
- **No profile reference.** Every run passes a profile computed from the same data it checks
  (the bindings compute it with `compute_profile=True`), so using `ctx.profile` would have
  put the broken segment into its own reference. The reference always comes from the leading
  segments; the profile carries the two fields for later consumers, and a stored baseline can
  become the reference when one exists.
- A 7-day segment cannot hold a weekly rhythm (three periods are needed), so a daily series
  that turns into a weekday/weekend pattern is `weaker` with the defaults and
  `period_changed` with `segment` 28d. Weekly → daily is `period_changed` with the defaults.
- The reference period needs at least half of the reference segments to agree (2 of 4), the
  spec's "modal period" without ties to break. Segments are anchored at multiples of their
  length since the epoch (Thursdays for 7 d), as spec 009's are, and need half their bins.
- A segment that shows a new period with strength ≥ `min_strength` is `period_changed` even
  when its reference-period strength also fell; an episode's `reason` is the majority of its
  segments (`period_changed` on a tie).

## Out of scope

- MSTL with several periods at once and STL robustness weights (R2; the catalogue's
  "augurs" note is replaced by this pure-Rust method, ADR-0015).
- Seasonal spike variant (S-H-ESD) in `tby.spikes`: later, once this profile field exists.
