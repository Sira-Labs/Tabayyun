# ADR-0011: Findings describe episodes, not samples; recurring behaviour is one finding

- **Status:** Accepted
- **Date:** 2026-09-21
- **Deciders:** core maintainers

## Context
Running the twenty implemented checks on public energy data produced thousands of findings
per series: 1,149 `tby.flatline` findings on OPSD Germany solar (one per night, generation is
legitimately zero), 500 capped `tby.spikes` and 363 `tby.distribution_drift` findings on the
UCI household active power (appliance switching is the signal), 146 `tby.changepoint`
findings on OPSD Germany load (weekday/weekend and seasonal level changes) and 99
`tby.noise_level` findings on the household series. Each finding was individually "correct"
by its check's definition and collectively useless: nobody triages a thousand nights.

Two forces pull against each other. A finding must stay a precise, evidence-backed statement
about a window, so that corrections (ADR-0010) and scores can be attached to it. A series
must yield a list short enough to act on, and the synthetic-fault unit tests (a two-hour
stuck sensor, five injected spikes, one shifted day, one step) must keep detecting the
faults they cover.

## Decision
1. **A finding is an episode.** Segment checks (`tby.distribution_drift`, `tby.noise_level`)
   merge consecutive flagged segments into one finding whose window spans them; evidence
   keeps the per-segment statistics and the count. Point checks (`tby.spikes`) merge spikes
   closer than `cluster_gap` into one finding per cluster with an exact count and the peak.
   The score impact of an episode equals the sum of its parts (affected fraction is the
   union of the windows), so scores do not change; only the number of findings does.
2. **Recurring behaviour is a property of the series, reported once.** When a series has
   more spike clusters or more level changes than `max_findings` (default 20), listing them
   cannot help: one low-severity summary finding states the count, the rate and the largest
   cases in evidence, and the metrics (`spike_count`, `spike_clusters`, `level_changes`)
   carry the exact numbers for trending. The same principle applies to `tby.flatline`: runs
   resting on the series' floor (physical minimum, zero for non-negative quantities, else
   the observed minimum) are idling when the floor is a recurring state (≥ 5 % of samples,
   ≥ 3 runs); such runs are counted in a metric, not reported, unless one is far longer than
   the median floor run.
3. **Per-segment thresholds are relative to the window's own segments once there are enough
   of them.** With ≥ 8 segments, a drift or noise segment is reported on its own only when
   it is both beyond the absolute threshold and unusual among its siblings (beyond 3 robust
   sigmas of the segment statistics). When the median segment is itself beyond the
   threshold, the whole window drifted against the baseline and that is one finding, so a
   reference-window baseline still catches a series that shifted everywhere.
4. **Point outliers are isolated.** A Hampel hit counts as a spike only in a run of at most
   `max_width` (3) consecutive hits; longer runs are excursions and belong to
   `tby.rate_of_change`, `tby.operational_range` and `tby.changepoint`.
5. **Regime changes persist and are abrupt.** `tby.changepoint` requires the new level to
   hold for a week of buckets, the buckets immediately around the change to differ as much
   as the segments do (rejects ramps cut into staircases) and an effect size ≥ 2 against the
   within-segment spread.

Result on the four series that motivated the change (all checks, default parameters):

| Series | Before | After |
|---|---|---|
| OPSD DE solar, hourly 2015–2020 | 2,016 | 7 |
| OPSD DE load, hourly 2015–2020 | 150 | 15 |
| UCI household active power, 1-min 2007 | 984 | 29 |
| UCI household voltage, 1-min 2007 | 876 | 21 |

All synthetic-fault unit tests still pass; each behaviour above has its own.

## Alternatives considered
| Option | Pros | Cons | Why not |
|---|---|---|---|
| Raise thresholds (`t`, `high`, `psi_alert`) | One-line change | Loses the injected faults the tests cover; the household series still yields hundreds | Sensitivity was not the problem, cardinality was |
| Cap findings per check (the previous `max_findings` 500 on spikes) | Bounded output | Arbitrary truncation loses the count and the worst cases | Not a summary, a cut |
| One finding per day for point checks | Simple | A 1-minute series with daily spikes still yields 365 findings | Does not bound the list |
| Seasonal baselines (same weekday, same hour) for drift and noise | Principled for cyclic series | Needs a seasonality model the profile does not yet have (check #21) | Deferred; the sibling rule needs no model |
| Longer persistence for changepoints (≥ 14 days) | Removes synoptic weather regimes | Also removes the 20-day synthetic step and two-week Christmas dips | Summary keeps both |

## Consequences
- Evidence schemas changed: spike findings carry `count`, `ts[]`, `values[]` and peak fields
  instead of one `ts`; drift and noise findings carry `segments` and per-segment arrays and a
  `whole_window` flag; changepoint findings carry `local_jump` and `effect_size`. Summary
  findings carry `summary_of` and `largest_clusters` / `largest_changes`. Consumers must
  not assume one finding per sample or per segment.
- New parameters (`ignore_floor`, `floor_fraction`, `min_floor_runs`, `floor_run_factor`,
  `max_width`, `cluster_gap`, `max_listed`, `min_segments_relative`, `unusual_sigmas`,
  `min_effect_size`, `max_findings` on changepoint) are documented in the catalogue.
- The UI should render episode counts and cluster counts, and offer "expand" on a summary
  finding to show the largest cases from evidence.
- Follow-up: the seasonality profile (check #21) can replace the sibling rule with a proper
  seasonal baseline for drift and noise; the whole-window drift finding should be suppressed
  when the profile is known to be computed on the evaluated window itself (self baseline),
  which the profile does not record today.
