# Spec 028 — Baseline profiles: stored, versioned, refreshed by a job

Sprint 10, story S10-2. Depends on:
- spec 006: the Parquet cache and the `coverage` table;
- spec 008: dataset runs;
- spec 025: the profile of a window and the series page;
- spec 027: check suites and the thresholds that name `baseline` as their source;
- spec 002: the job queue; spec 007: tenancy; spec 014: the audit log.

Packages: `core/tabayyun-core` (profile), `core/tabayyun-py` (bindings), `api/src/tabayyun/`
(model, migration 0014, service, routes, tasks, dataset-run executor), `web/src/` (series page).

## Goal

Every series with cached data has a baseline: the core's profile of its trailing 28 days,
without bad-quality samples and without the windows of its open critical findings. Baselines
are stored and versioned. A job refreshes them once a day; an editor can refresh one at once.
Dataset runs judge a series against its stored baseline instead of against the window they
are checking, and the series page shows the thresholds the baseline gives and how they
changed with the last refresh.

## User story

As a data steward, I look at a series whose rate-of-change findings look too eager. Its page
says the limit is 0.42 per second, learned from the baseline of last night, and that the
baseline before gave 0.31. After a maintenance week is over I press "Refresh baseline" and
see the limit change, without waiting for the night.

## Interface

### Core and bindings

- `Profile` deserialises with missing fields as their defaults, so a stored profile stays
  readable when a later version adds a statistic.
- `tabayyun_core.run_checks_multi(..., profiles=None)`: `{series_id: profile}`. A series
  listed there is checked against that profile; any other series against the profile of its
  own data in the window, as before. The report's `profile` is the one that was used.

### Database (migration 0014)

`series_baselines` (tenant table, row-level security by `org_id`):

| Column | Type | Meaning |
|---|---|---|
| `id` | uuid | primary key |
| `series_id` | uuid | the series; its deletion deletes its baselines |
| `version` | integer | 1, 2, … per series; unique with `series_id` |
| `window_start`, `window_end` | timestamptz | the window that was read |
| `n_samples` | integer | samples the profile was computed from |
| `excluded` | jsonb | `{"bad": n, "findings": n, "windows": [[start_ns, end_ns], …]}` |
| `profile` | jsonb | the core's profile |
| `trigger` | text | `schedule` or `manual` |
| `created_at` | timestamptz | when it was computed |

`series.baseline_checked_at` (timestamptz, null): when the scheduler last queued the series.

`tabayyun_claim_series_for_baseline(now, limit)` (SECURITY DEFINER, callable by
`tabayyun_app` only): sets `baseline_checked_at = now` on up to `limit` series that have raw
coverage and were not queued in the last 24 hours, longest-waiting first, and returns their
`(series_id, org_id)`.

### API

- `GET /api/series/{id}/baselines?limit=` (viewer) → `{items: [Baseline], next_cursor: null}`,
  newest first, at most 30. The first item is the current baseline.
- `POST /api/series/{id}/baselines` (editor) → 202 `{"queued": true}`: computes a baseline
  now. 409 `no_data` when the cache holds nothing of the series.

```json
{
  "version": 7,
  "created_at": "2026-11-17T02:37:04Z",
  "trigger": "schedule",
  "window": {"from": "1760572800000000000", "to": "1762992000000000000"},
  "n_samples": 40212,
  "excluded": {"bad": 12, "findings": 96, "windows": [["1761000000000000000", "1761003600000000000"]]},
  "thresholds": [
    {"check_id": "tby.rate_of_change", "param": "max_rate", "value": 0.42,
     "basis": "3 × the 99.9th percentile rate (0.14 per second)"},
    {"check_id": "tby.spikes", "param": "min_scale", "value": 0.05,
     "basis": "the larger of the resolution (0.01) and the noise floor (0.05)"}
  ],
  "band": {"lo": 3.1, "hi": 96.4, "source": "profile"},
  "profile": {"…": "the core's profile"}
}
```

`thresholds` holds one entry for every parameter the check catalogue marks as learned from
the baseline for a single series (spec 027's `auto` with source `baseline`, scope `series`),
computed with the check's default parameters; `value` is null when the baseline has nothing
to learn it from. `band` is the operating band of spec 025 for this profile.

- Dataset runs record the baselines they used: `stats.baselines` is `{series_id: version}`.

### Web

The series page gets a "Baseline" panel: version, when and why it was computed, its window,
how many samples it used and how many it left out, and the learned thresholds, each with the
value of the version before and the change. Editors get "Refresh baseline". Without a
baseline the panel says so and, for editors, offers to compute one.

## Behaviour

1. **What a baseline is computed from.** The window is the trailing 28 days of what the cache
   holds of the series, ending at its last covered instant (not at the time of the job, so an
   uploaded history gets a baseline too). When the coverage says the window holds more than
   `TABAYYUN_BASELINE_MAX_ROWS` rows (default 5 000 000), the window is halved (14 days, 7
   days, … one day) until it fits; if one day does not fit, no baseline is computed. Left out
   are samples of bad quality, and samples inside the window of a finding of the series that
   is `open` and `critical`. Excluded windows are recorded.
2. **Too little to learn from.** With fewer than 50 samples left, no baseline is stored and
   the current one stays. The job logs why.
3. **Versions.** A computed profile that equals the current one (nothing new arrived, nothing
   was excluded or released) stores no new version. Otherwise it is stored with the next
   version number. The newest 30 versions of a series are kept.
4. **The scheduler.** Every hour a periodic task claims up to 200 series through the claim
   function and queues one `profile_series` job each. The jobs share a lock, so they run one
   after another and take at most one worker slot. Each series is therefore refreshed about
   once a day.
5. **Refresh now** queues the same job with trigger `manual` (run after the response with
   inline jobs) and is written to the audit log as `series.baseline_refresh_requested`.
6. **Runs.** A dataset run loads the current baseline of each of its series and passes it to
   the core. A series without one is judged against the profile of the run's window, as
   before. Upload runs are unchanged.
7. **Failures.** A cache error fails the job without a new version; the next day's job tries
   again. A series deleted in between is skipped.

## Acceptance criteria

- [x] A stored profile from which a field was removed still loads in the core.
- [x] `profile_series` stores version 1 for a series with cached data, with the trailing 28
      days as its window, and nothing for a series with fewer than 50 usable samples.
- [x] Bad-quality samples and the windows of open critical findings are left out and
      recorded; a resolved or non-critical finding is not left out.
- [x] A second job without new data stores no new version; with new data it stores version 2;
      only the newest 30 are kept.
- [x] The hourly task queues each series with coverage once in 24 hours, in every org, and
      the jobs carry the shared lock.
- [x] A dataset run uses the stored baseline: with a baseline whose `rate_p999` is tiny, a
      window of ordinary data gives rate-of-change findings that the same run without a
      baseline does not; `stats.baselines` names the version.
- [x] `GET …/baselines` returns versions newest first with thresholds and band; the learned
      thresholds differ between two versions computed from different data.
- [x] `POST …/baselines` is refused for viewers (403), is 409 `no_data` without cached data,
      and is audited.
- [x] The series page shows the baseline panel with the change against the previous version,
      and "Refresh baseline" leads to a new version on the page.
- [x] Another org's series gives 404 on both routes.

## Test cases

- **Core** (`profile::tests`): `profile_deserialises_with_missing_fields`.
- **Bindings**: `test_run_checks_multi_uses_given_profiles`.
- **API** (`api/tests/db/test_baselines.py`): `first_baseline_and_too_few_samples`,
  `exclusions_bad_quality_and_open_critical_findings`, `versions_no_change_new_data_pruning`,
  `window_shrinks_to_the_row_limit`, `thresholds_follow_the_profile`,
  `routes_roles_no_data_and_audit`, `scheduler_claims_once_a_day_with_the_lock`,
  `dataset_run_uses_the_stored_baseline`.
- **Tenancy**: both routes join `ROUTES` in `tests/db/test_rls.py`; migration 0014 upgrades
  and downgrades; the claim function is callable by the app role only.
- **Web** (Vitest): `series.baseline` (panel with and without a baseline, change against the
  previous version, refresh for editors only, a new version appears after a refresh).

## Implementation notes

- The thresholds are a list, not a table: the panel sits in the series page's narrow side
  column, and a six-column table hid the previous value and the change behind a sideways
  scroll (seen in the browser check).
- A refresh that changes nothing stores no version, so the panel waits 60 seconds and then
  says that nothing changed or that there is too little usable data; the job's own outcome is
  not reported back to the page.
- The declared resolution of the series wins over the learned one in the spike floor, as in
  the check, so that threshold can change with a metadata edit, without a new version.
- `stats.baselines` is present on every dataset run, empty when no series had a baseline.
- The retry test of migration 0013 (spec 027) upgraded to `head`; it now names 0013, so later
  migrations do not run inside it.

## Out of scope

- Baselines for upload runs and for the `/profile` route of an arbitrary window (spec 025).
- Learned tolerances of groups (`tby.redundant_disagreement`): they are learned from the
  members' spread inside the run, not from one series' profile.
- Choosing the baseline window or pinning a version per series: later, with packs (S12).
- Showing the learned number on the suite page: a suite covers many series; its thresholds
  keep naming the source, and the series page shows the number.
