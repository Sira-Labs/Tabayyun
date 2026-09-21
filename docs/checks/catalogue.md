# Check catalogue — the first 30

The R1 catalogue. Selection criteria: frequency in real energy/historian data × downstream
damage × implementability in Rust without ML infrastructure. 24 generic checks that run on
any series, 6 energy-pack checks that need domain metadata. Every check follows
`docs/checks/00-check-specification.md`. Evidence for defaults is in
`docs/research/01-sota-timeseries-quality.md` and `docs/research/02-energy-domain-quality.md`.

Legend: **Dim** = primary quality dimension. **Needs** = required series metadata or related
series. **Sev** = default severity. **Auto** = default is learned from the baseline profile.

## Overview

Status column: ✅ implemented in `core/tabayyun-core/src/checks/`, ⬜ planned.

| # | id | Title | Dim | Needs | Sev | Status |
|---|---|---|---|---|---|---|
| 1 | `tby.completeness` | Gaps and missing samples | completeness | expected_interval (auto) | high | ✅ |
| 2 | `tby.staleness` | Series not updating | timeliness | expected_interval (auto) | high | ✅ |
| 3 | `tby.latency` | Late arrival and future timestamps | timeliness | ingest_ts | medium | ✅ |
| 4 | `tby.timestamp_integrity` | Duplicates and out-of-order timestamps | integrity | — | high | ✅ |
| 5 | `tby.sampling_regularity` | Interval change, jitter, wrong count per day | integrity | expected_interval (auto), timezone | medium | ✅ |
| 6 | `tby.quality_flags` | Bad/uncertain/estimated flag share and frozen-good | validity | quality column | high | ✅ |
| 7 | `tby.value_type` | NaN, Inf, non-numeric, dtype change | validity | — | high | ✅ |
| 8 | `tby.flatline` | Stuck / frozen values | plausibility | kind, resolution (auto) | high | ✅ |
| 9 | `tby.physical_range` | Outside physically possible limits | validity | physical_min/max or unit | critical | ✅ |
| 10 | `tby.operational_range` | Outside learned operating band | plausibility | baseline (auto) | medium | ✅ |
| 11 | `tby.non_negative` | Negative values for non-negative quantities | validity | unit / kind | high | ✅ |
| 12 | `tby.scale_shift` | Unit or scale error (×10, ×1000, °C↔°F) | validity | baseline (auto) | critical | ✅ |
| 13 | `tby.spikes` | Point outliers (Hampel) | plausibility | baseline (auto) | medium | ✅ |
| 14 | `tby.rate_of_change` | Slew-rate violation | plausibility | max_rate or baseline (auto) | medium | ✅ |
| 15 | `tby.noise_level` | Variance jump or suspicious smoothness | plausibility | baseline (auto) | medium | ✅ |
| 16 | `tby.resolution_loss` | Quantization / precision drop | validity | baseline (auto) | medium | ✅ |
| 17 | `tby.interpolation_artifacts` | Linear runs from compression/interpolation | validity | baseline (auto) | medium | ✅ |
| 18 | `tby.level_drift` | Slow bias / trend in the level | accuracy | baseline (auto) | medium | ✅ |
| 19 | `tby.distribution_drift` | Distribution changed vs reference | plausibility | baseline (auto) | medium | ✅ |
| 20 | `tby.changepoint` | Abrupt regime change | plausibility | — | medium | ✅ |
| 21 | `tby.seasonality_break` | Periodic pattern lost or changed | plausibility | baseline (auto) | low | ⬜ |
| 22 | `tby.correlation_break` | Related series stopped agreeing | consistency | related series | high | ⬜ |
| 23 | `tby.redundant_disagreement` | Redundant sensors disagree | accuracy | redundant series, tolerance | high | ⬜ |
| 24 | `tby.balance_residual` | Energy/mass balance violated | consistency | balance group definition | high | ⬜ |
| 25 | `energy.metering.register_reconciliation` | Interval sum ≠ register advance | consistency | register series, multiplier | high | ⬜ |
| 26 | `energy.metering.usage_plausibility` | Zero runs, high/low vs history, reactive-without-active | plausibility | paired kvarh (optional) | medium | ⬜ |
| 27 | `energy.pv.irradiance_limits` | Irradiance outside BSRN limits / component inconsistency | validity | lat/lon, timestamps in UTC; DNI/DHI optional | high | ⬜ |
| 28 | `energy.pv.time_shift` | Timezone/DST error via solar-noon offset | integrity | lat/lon | critical | ⬜ |
| 29 | `energy.pv.clipping` | Inverter clipping / curtailment not flagged | plausibility | ac_capacity | low | ⬜ |
| 30 | `energy.wind.power_curve_outlier` | Power vs wind speed off the curve; curtailment | plausibility | wind_speed series, rated_power | medium | ⬜ |

## Baseline profile

Most defaults are derived from a per-series **baseline profile** computed on a reference
window (default: trailing 28 days, excluding Bad-quality points and windows with open
critical findings):

| Profile field | Method |
|---|---|
| `expected_interval` | mode of inter-arrival times (IAT), rounded to a "nice" duration |
| `iat_p50, iat_p99` | robust IAT quantiles |
| `median, mad, p001, p01, p99, p999` | robust value statistics |
| `resolution` | smallest non-zero |Δ| over the window, and distinct-value count |
| `rate_p999` | 99.9th percentile of |Δvalue/Δt| |
| `noise_mad` | MAD of first differences |
| `dominant_period, seasonal_strength` | augurs seasonality detection + MSTL |
| `acf1` | lag-1 autocorrelation |
| `quality_mix` | share of good/uncertain/bad/estimated |
| `constant_fraction` | fraction of samples in runs longer than 10 samples |

Profiles are versioned and stored; findings link to the profile they used.

---

## Generic checks

### 1. `tby.completeness` — Gaps and missing samples
- **Dim:** completeness. **Sev:** high.
- **Algorithm:** IAT = ts[i] − ts[i−1]. A gap is IAT > `gap_factor × expected_interval`.
  Completeness ratio = observed samples / expected samples per evaluation window (hour/day),
  counting only raw (non-interpolated) samples if the source marks them.
- **Params:** `gap_factor` 3; `min_gap` 5 min; `min_completeness` 0.95 per day; for PV
  daytime-only windows use `daylight_only: true`.
- **Evidence:** list of gaps (start, end, duration), completeness per window, expected_interval.
- **Sources:** arXiv 2501.07154; AEMO §10.2(d); NASPI PARTF (gap rate, mean gap, largest gap);
  pvanalytics `gaps.complete(0.333)`.

### 2. `tby.staleness` — Series not updating
- **Dim:** timeliness. **Sev:** high.
- **Algorithm:** age = now − newest good-quality timestamp. Stale if age > `max_age`.
  Evaluated continuously (not only per run); emits a metric `stale_age`.
- **Params:** `max_age` auto = max(3 × expected_interval, 5 min); `ignore_quality_bad` true.
- **Evidence:** newest_ts, age, expected_interval.
- **Sources:** TrendMiner "Delayed" 4 h; Timeseer stale KPI; AVEVA PI Square stale-tag pattern.

### 3. `tby.latency` — Late arrival and future timestamps
- **Dim:** timeliness. **Sev:** medium.
- **Algorithm:** latency = ingest_ts − event_ts. Flag windows where p95 latency > `sla`.
  Separately flag event_ts > ingest_ts + `future_tolerance` (clock ahead).
- **Params:** `sla` auto = 2 × expected_interval; `future_tolerance` 60 s.
- **Evidence:** latency quantiles, count of future-stamped samples, max lead.
- **Sources:** NASPI PARTF latency attribute; UBP time tolerance 3 min; Elexon 15 min.

### 4. `tby.timestamp_integrity` — Duplicates and out-of-order
- **Dim:** integrity. **Sev:** high.
- **Algorithm:** exact duplicate (same ts, same value) → count and drop; conflicting
  duplicate (same ts, different value) → finding; non-monotonic ts (Δt < 0) → finding.
  Detect the leap-second/DST "duplicate hour" pattern separately (see #5).
- **Params:** `allow_exact_duplicates` true; `conflict_tolerance` = resolution.
- **Evidence:** counts, first 20 offending timestamps, distinct values at conflicts.
- **Sources:** arXiv 2501.12720; OpenOA `duplicate_time_identification`; NASPI leap-second incident.

### 5. `tby.sampling_regularity` — Interval change, jitter, wrong count per day
- **Dim:** integrity. **Sev:** medium.
- **Algorithm:** (a) regularity score = share of IAT within ±`jitter_tol` of
  expected_interval; (b) changepoint on rolling modal IAT (sampling rate changed);
  (c) for regular-interval series, expected samples per local day = 86400/interval,
  except DST days (e.g. 92/100 quarter-hours, 46/50 half-hours); a mismatch on non-DST days
  or the wrong pattern on DST days flags a timezone handling error.
- **Params:** `jitter_tol` 10 %; `min_regularity` 0.9; `timezone` from workspace/series.
- **Evidence:** regularity, old/new interval, per-day sample counts around DST dates.
- **Sources:** arXiv 2501.07154; BDEW MaBiS 96/92/100; OPSD DST notes; Streamkap DST artefacts.

### 6. `tby.quality_flags` — Flag share and frozen-good
- **Dim:** validity. **Sev:** high.
- **Algorithm:** map source quality (OPC UA StatusCode, PI, Canary, meter A/E, AEMO
  substitution types, PMU STAT word) to Tabayyun quality = good | uncertain | bad |
  estimated. Flag windows with bad > `max_bad`, uncertain > `max_uncertain`, estimated >
  `max_estimated` per month, or estimated runs > `max_estimated_runs` per year. Frozen-good:
  quality is good while #8 or #2 fires → separate finding "quality flag not trustworthy".
- **Params:** `max_bad` 1 %; `max_uncertain` 5 %; `max_estimated` 10 %/month;
  `max_estimated_runs` 3/year.
- **Evidence:** mix, worst windows, mapping used.
- **Sources:** OPC UA Part 8; Canary/Cognite status codes; UBP ≤10 %/month, ≤3 estimations/12 months; IEEE C37.118.2 STAT bits.

### 7. `tby.value_type` — NaN, Inf, non-numeric, dtype change
- **Dim:** validity. **Sev:** high.
- **Algorithm:** count NaN/Inf/unparseable; detect dtype change vs catalogue (numeric →
  string, float → bool). Any Inf = finding; NaN ratio > `max_nan`.
- **Params:** `max_nan` 1 %.
- **Evidence:** counts, examples, observed dtype.

### 8. `tby.flatline` — Stuck / frozen values
- **Dim:** plausibility. **Sev:** high.
- **Algorithm:** run length of |Δ| ≤ `atol` (default resolution/2) ≥ `min_run` samples
  **and** ≥ `min_duration`. Skip if series kind is setpoint/status or the profile's
  `constant_fraction` > 0.5 (legitimately constant), unless overridden. Compression-aware:
  if the source uses deadband/swinging-door compression, a flat run with no archived points
  is not evidence of stuck; require archived samples inside the run.
- **Params:** `min_run` 6 (pvanalytics) to 10; `min_duration` auto = max(1 h, 10 × interval);
  `atol` auto; OpenOA uses 3 intervals for 10-min SCADA.
- **Evidence:** run start/end, run length, value, resolution, compression mode.
- **Sources:** pvanalytics `stale_values_diff(window=6)`; OpenOA `unresponsive_flag(3)`; AVEVA `Range()==0`; PMU flat 60.00 Hz.

### 9. `tby.physical_range` — Outside physically possible limits
- **Dim:** validity. **Sev:** critical.
- **Algorithm:** value < physical_min or > physical_max. Limits from metadata, else from
  unit defaults (%, 0–100; K > 0; RH 0–100; kWh per interval ≤ capacity × interval; power ≤
  rated capacity × `capacity_factor_max`).
- **Params:** `physical_min/max`; `capacity_factor_max` 1.1.
- **Evidence:** count, min/max observed, limit, source of limit.
- **Sources:** Timeseer "improbable limits"; Elexon CoP max kWh table; AEMO CT-ratio max; OpenOA `range_flag`.

### 10. `tby.operational_range` — Outside learned operating band
- **Dim:** plausibility. **Sev:** medium.
- **Algorithm:** band = [p0.1 − k·MAD, p99.9 + k·MAD] from baseline (or median ± 5·MAD);
  finding if share out of band > `max_share` per window.
- **Params:** `k` 1; `max_share` 0.5 %.
- **Evidence:** band, share, worst excursions.
- **Sources:** robust statistics practice; Timeseer operational limits.

### 11. `tby.non_negative` — Negative values for non-negative quantities
- **Dim:** validity. **Sev:** high.
- **Algorithm:** for kinds/units flagged non-negative (flow, level, concentration, energy
  import, irradiance with −4 W/m² tolerance), value < −`tolerance` → finding.
- **Params:** `tolerance` = resolution (irradiance: 4 W/m² per BSRN).
- **Sources:** MHHS §2.25; Oracle Negative Consumption Check; BSRN min limits.

### 12. `tby.scale_shift` — Unit or scale error
- **Dim:** validity. **Sev:** critical.
- **Algorithm:** ratio r = median(window)/median(baseline). Flag if r within ±10 % of
  {10, 100, 1000, 0.1, 0.01, 0.001} or if window fits `1.8·x + 32` (°C→°F) better than
  identity by `min_gain`. Also fires on step change detected by #20 with a ratio in the set.
- **Params:** `ratio_tol` 10 %; `min_gain` 0.9 R².
- **Evidence:** ratio, candidate transformation, window.

### 13. `tby.spikes` — Point outliers
- **Dim:** plausibility. **Sev:** medium.
- **Algorithm:** Hampel filter: |x − rolling_median| > `t` × 1.4826 × rolling_MAD, window
  `w`. Optional seasonal variant: STL residual + generalized ESD when
  `seasonal_strength` > 0.6. Energy-metering variant (UBP): per 24 h,
  (highest − 3rd highest)/3rd highest > 1.8 and highest > 10 pulses.
- **Params:** `t` 3.0 (pvanalytics hampel 3.0); `w` 11 samples; `ubp_ratio` 1.8.
- **Evidence:** spike timestamps, values, local median/MAD.
- **Sources:** Hampel (pracma/MATLAB); S-H-ESD; UBP §1.4.4; Oracle Interval Spike Check.

### 14. `tby.rate_of_change` — Slew-rate violation
- **Dim:** plausibility. **Sev:** medium.
- **Algorithm:** |Δx/Δt| > `max_rate`; default `max_rate` = 3 × baseline `rate_p999`, or
  physical slew limit from metadata.
- **Evidence:** events, observed rate, limit.
- **Sources:** IEC 61724-3 abrupt-change filter via derivatives.

### 15. `tby.noise_level` — Variance jump or suspicious smoothness
- **Dim:** plausibility. **Sev:** medium.
- **Algorithm:** ratio = rolling MAD of first differences / baseline `noise_mad`. Noisier if
  > `high`; too smooth (filtered, interpolated, compression changed) if < `low`.
- **Params:** `high` 2.0; `low` 0.3; window 1 day.
- **Sources:** Timeseer variance drift; compression research (arXiv 2510.26868).

### 16. `tby.resolution_loss` — Quantization / precision drop
- **Dim:** validity. **Sev:** medium.
- **Algorithm:** distinct-value count and min non-zero |Δ| per window vs baseline; finding
  if distinct count < `min_distinct_ratio` × baseline or step > `step_factor` × baseline.
- **Params:** `min_distinct_ratio` 0.2; `step_factor` 5.

### 17. `tby.interpolation_artifacts` — Linear runs from compression/interpolation
- **Dim:** validity. **Sev:** medium.
- **Algorithm:** second difference ≈ 0 for ≥ `window` consecutive samples marks a linear
  run (pvanalytics `interpolation_diff`); share of samples in linear runs > `max_share`.
  Also compare IAT to scan rate for deadband-compressed sources (IAT ≫ scan rate with
  perfectly linear segments = over-aggressive CompDev).
- **Params:** `window` 6; `max_share` 20 % (daytime for PV).
- **Sources:** pvanalytics gaps.interpolation_diff; AVEVA exception/compression guidance.

### 18. `tby.level_drift` — Slow bias / trend
- **Dim:** accuracy. **Sev:** medium.
- **Algorithm:** Theil–Sen slope of daily medians over `horizon`; drift if
  |slope × horizon| > `k` × baseline MAD. Online alternative: Page–Hinkley on residuals.
- **Params:** `horizon` 30 d; `k` 3.
- **Sources:** river Page–Hinkley; sensor-drift literature (Teh 2020).

### 19. `tby.distribution_drift` — Distribution changed vs reference
- **Dim:** plausibility. **Sev:** medium.
- **Algorithm:** per segment (default one day) compare usable values with the baseline's
  21-point quantile grid: PSI over 10 bins bounded by the baseline deciles (skipped when the
  deciles are tied, e.g. constant or heavily quantised baselines), and a Wasserstein-1
  distance approximated on the quantile grid and normalised by the baseline inter-quartile
  range. Findings on PSI ≥ alert or Wasserstein ≥ alert (medium); PSI ≥ warn alone is low.
  Excludes windows with open changepoint findings that the user accepted as legitimate
  (re-baseline).
- **Params:** `psi_warn` 0.1, `psi_alert` 0.25, `wasserstein_alert` 0.1, `segment_ns` 1 d,
  `min_samples` 100.
- **Sources:** Evidently defaults; PSI literature; DQSOps drift-aware re-baselining.

### 20. `tby.changepoint` — Abrupt regime change
- **Dim:** plausibility. **Sev:** medium.
- **Algorithm:** offline PELT with `l2` cost and BIC penalty on daily-aggregated series
  (augurs/ruptures-equivalent); online BOCPD for streaming. Emits changepoints as findings
  requiring triage: "legitimate (re-baseline)" or "data problem".
- **Params:** `penalty` BIC; `min_segment` 1 day.
- **Sources:** ruptures PELT; Adams & MacKay BOCPD; pvanalytics `detect_data_shifts`.

### 21. `tby.seasonality_break` — Periodic pattern lost
- **Dim:** plausibility. **Sev:** low.
- **Algorithm:** if baseline `seasonal_strength` > 0.6, compute strength and dominant
  period on the window; finding if strength drops by > 50 % or period changes.
- **Sources:** STL/MSTL (augurs); ydata-profiling seasonality alerts.

### 22. `tby.correlation_break` — Related series stopped agreeing
- **Dim:** consistency. **Sev:** high.
- **Algorithm:** rolling Spearman ρ between the series and each configured related series
  (auto-suggested from baseline |ρ| > 0.8 within the same asset); finding if
  |ρ_now − ρ_ref| > `delta` or sign flips. Lag drift: cross-correlation lag shifts by > 1 sample.
- **Params:** `delta` 0.3; window 1 day.
- **Sources:** Timeseer broken correlations; Li 2022.

### 23. `tby.redundant_disagreement` — Redundant sensors disagree
- **Dim:** accuracy. **Sev:** high.
- **Algorithm:** |A − B| > `tolerance` for ≥ `min_duration`; with ≥3 sensors, median
  voting identifies the outlier. Tolerance from class accuracy (meters: 1.5 × class
  accuracy at full load) or metadata.
- **Params:** `tolerance`; `min_duration` 15 min.
- **Sources:** Elexon main/check 1.5 × class; AEMO check meter 5 %.

### 24. `tby.balance_residual` — Energy/mass balance violated
- **Dim:** consistency. **Sev:** high.
- **Algorithm:** for a balance group (inputs, outputs, expected loss band), residual
  r = Σin − Σout; normalised residual > `k` σ or loss share outside [`loss_min`, `loss_max`].
  Gross-error detection via measurement test to point at the offending series.
- **Params:** `k` 3; nodal balance 1 % (AEMO); DSO loss band per network (CEER 2–23 %);
  CGMES interchange 50/200 MW.
- **Sources:** AEMO §10.3; CEER losses; ENTSO-E QoCDC; data-reconciliation literature.

---

## Energy pack (R1 subset)

### 25. `energy.metering.register_reconciliation` — Interval sum vs register advance
- **Dim:** consistency. **Sev:** high. **Needs:** register series, multiplier, rollover value.
- **Algorithm:** Σ intervals between two register reads vs (R_end − R_start) with rollover;
  fail if |diff| > max(`multipliers` × meter multiplier, `pct` × advance).
- **Params:** `multipliers` 2 (UBP); `pct` 0.7 % weekly / 5 % daily (Elexon); 0.1 % site-read (MHHS).
- **Sources:** UBP §1.4.3; BSCP502 App.4.1.5; MHHS METH002 §2.16.

### 26. `energy.metering.usage_plausibility` — Zeros, high/low, reactive-without-active
- **Dim:** plausibility. **Sev:** medium.
- **Algorithm:** (a) zero intervals per day > baseline p99 of zeros/day; (b) average daily
  usage vs same month last year (or trailing month) deviates > `pct`; (c) kWh = 0 while
  kvarh > `reactive_pulses` in the same interval.
- **Params:** `pct` 25 % (UBP); `reactive_pulses` 4 (UBP).
- **Sources:** AEMO §10.2(c); UBP §1.4.5, §1.5.1; Oracle High/Low.

### 27. `energy.pv.irradiance_limits` — BSRN limits and component consistency
- **Dim:** validity. **Sev:** high. **Needs:** lat/lon, UTC timestamps; DNI/DHI optional.
- **Algorithm:** solar position (pvlib-equivalent in Rust) → Sa, μ0. Physically possible:
  GHI ≤ Sa·1.5·μ0^1.2 + 100, DHI ≤ Sa·0.95·μ0^1.2 + 50, DNI ≤ Sa, min −4 W/m². Extremely
  rare tier as warning. Consistency when GHI > 50: GHI/(DNI·cosZ + DHI) within ±8 % (SZA <
  75°) / ±15 %; DHI/GHI < 1.05 / 1.10. Night-time non-zero and clear-sky index > 1.1.
- **Sources:** BSRN QC V2; pvanalytics `irradiance` module.

### 28. `energy.pv.time_shift` — Timezone/DST error via solar-noon offset
- **Dim:** integrity. **Sev:** critical. **Needs:** lat/lon.
- **Algorithm:** daily solar-noon estimate from the power/irradiance curve vs modelled
  solar noon; changepoint on the offset series (PELT); finding if a segment's offset
  > `shift_min`. Detect DST pattern (offset alternates by 60 min with the DST calendar).
- **Params:** `shift_min` 15 min; `zscore_cutoff` 2.
- **Sources:** pvanalytics `time.shifts_ruptures`, `has_dst`; NREL time-shift study.

### 29. `energy.pv.clipping` — Inverter clipping / curtailment not flagged
- **Dim:** plausibility. **Sev:** low. **Needs:** ac_capacity.
- **Algorithm:** daily 99.5th-percentile power curve; flat top where slope < `slope_max` and
  power > `power_min` × capacity → clipped periods. Emits a metric (clipped share) and a
  finding when clipping periods exist but no curtailment/status flag accompanies them.
- **Params:** `slope_max` 0.0035; `power_min` 0.75; `power_quantile` 0.995.
- **Sources:** pvanalytics `clipping.threshold`.

### 30. `energy.wind.power_curve_outlier` — Off the power curve; curtailment
- **Dim:** plausibility. **Sev:** medium. **Needs:** wind_speed series, rated_power.
- **Algorithm:** bin power by wind speed (0.5 m/s bins); flag points > `k`σ from the bin
  mean (`bin_filter`); curtailment/stoppage: wind in [cut-in, cut-out] and power ≈ 0 for ≥
  `min_duration` without a matching status code; implausible stats: 10-min min > mean, std < 0.
- **Params:** `k` 2; `min_duration` 30 min.
- **Sources:** OpenOA `bin_filter`, `cluster_mahalanobis_2d(3.0)`; CARE dataset filters; IEC 61400-12-1 method of bins.

---

## Suggested repair per check

Each finding offers the repair operations that make sense for it; the user or a RepairFlow
chooses. Operations are implemented in `core::repair` and always produce lineage.

| Finding from | Offered operations (default first) |
|---|---|
| 1 completeness, 4 timestamp_integrity | `impute.linear` (gaps ≤ 2 h), `impute.like_day` (longer gaps, metering), `impute.seasonal` (strong seasonality), `impute.kalman`; duplicates: `dedupe.keep_first` / `dedupe.mean` |
| 5 sampling_regularity, 28 pv.time_shift | `align.resample` to expected interval; `align.shift` by detected offset; `align.timezone` reassign |
| 6 quality_flags | `mask.by_quality` (drop bad/uncertain), then imputation as above |
| 7 value_type, 9 physical_range, 11 non_negative, 27 pv.irradiance_limits | `mask.window` (drop), `clamp` to limits, `impute.*` |
| 8 flatline, 17 interpolation_artifacts | `mask.window`, `impute.*` (never keep the flat run as real data) |
| 12 scale_shift | `transform.scale` / `transform.affine` (undo ×1000, °F→°C) on the affected segment |
| 13 spikes, 14 rate_of_change | `mask.points` then `impute.linear`; `filter.hampel` for automated flows |
| 15 noise_level | `filter.median` / `filter.savgol` (produces a smoothed layer, raw kept) |
| 18 level_drift, 23 redundant_disagreement | `transform.offset` (bias removal vs reference), `replace.with_reference` (use redundant sensor) |
| 20 changepoint | no repair; triage decides "accept and re-baseline" or "mask segment" |
| 24 balance_residual, 25 register_reconciliation | `reconcile.balance` (data reconciliation distributing the residual by measurement uncertainty), `replace.with_register_apportionment` |
| 29 pv.clipping, 30 wind.power_curve_outlier | `mask.window` tagged as curtailment/clipping (excluded from performance KPIs, not treated as missing) |

## Implementation order (R1)

| Sprint | Checks | Why first |
|---|---|---|
| 1 | 1, 2, 4, 5, 7, 8, 9, 11 | Structural checks; no baseline needed; catch the majority of real incidents |
| 2 | baseline profile; 6, 10, 13, 14, 16, 17 | Profile unlocks adaptive thresholds |
| 3 | 3, 12, 15, 18, 19, 20 | Drift/change family |
| 4 | 21, 22, 23, 24 | Cross-series; needs related-series metadata |
| 5 | 25–30 | Energy pack; needs asset metadata (lat/lon, capacity, registers) |
| 6 | repair ops: mask, clamp, dedupe, impute.linear, impute.like_day, align.resample, transform.affine | Manual corrections from the chart with lineage, corrected layer, re-scoring |

## Deferred to R2+ (from the research list)

Forecast-residual anomalies (foundation models), subsequence/pattern anomalies (Matrix
Profile), soft-sensor residuals, calibration-event detection, fleet/population baselines,
digital-tag chattering, metadata validity (catalogue-level), row-count anomalies (source
level), PMU STAT-word decoding and TVE, CGMES SHACL validation, BESS SOC/cell-spread checks,
district-heating supply/return consistency, market-data interval count per delivery day.
