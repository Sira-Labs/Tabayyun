# Spec 036 — Reading a layer, and the corrected layer's score

Sprint 11, story S11-3. Depends on:
- spec 035 (corrections, `corrected/v{n}` in the cache, the composed corrected layer);
- spec 025 (series chart and profile), spec 018 and 033 (compare chart, residual, scatter);
- spec 009 (dataset runs and how they score), spec 032 (`score_days`);
- ADR-0010 (reads are opt-in: every read names a layer, the default is raw).

Packages: `api/src/tabayyun/` (layer names, the reading routes, the re-scoring job, score
fields), `docs/`.

## Goal

Every endpoint that reads samples takes a `layer`: `raw` (the default), `corrected@latest`
or `corrected@v{n}`. Nobody gets corrected data without asking. When a correction is
approved, the series' corrected layer is scored with the checks of the series' last run, and
the raw layer is scored the same way beside it, so the API says "raw 61 → corrected 94".

## User story

As a metering analyst, I approve the correction that fills last Tuesday's gap and see on the
series that its score went from 61 to 94 with the same checks, and I look at the corrected
curve in the chart by switching the layer, with the raw one still the default.

## Interface

### Layer names

`raw`, `corrected@latest`, `corrected@v{n}` (n ≥ 1, no leading zero). Anything else is 422
`{"detail": [{"field": "layer", "message": …}]}`.

- `corrected@latest` is the series' corrected layer at its highest approved version; a
  series without an approved correction answers with its raw rows.
- `corrected@v{n}` is the layer at version n: raw with the approved corrections up to n laid
  over (spec 035, behaviour 3). n must be an approved version of that series, otherwise 422
  `unknown_layer_version`. It is accepted where one series is read (chart, profile); the
  endpoints that read several series take `raw` and `corrected@latest` only, because
  versions count per series (422 otherwise).

### Reading routes

`layer` is a query parameter of

- `GET /api/series/{id}/chart` and `GET /api/series/{id}/profile`,
- `GET /api/compare/chart`, `GET /api/compare/scatter`,
- `GET /api/series-groups/{group_id}/residual`.

The answers of chart and profile keep their `layer` field (the name that was asked for) and
get `layer_version`: the version that was read, null for raw and for a series without an
approved correction. The compare answers get `layer` and, per series, `layer_version`. ETags
include the version, so a client's cached raw answer is never taken for a corrected one and
an approval changes the tag. Default windows, extents and the row estimate stay those of the
raw layer's coverage; the limit of spec 025 is checked against it.

### Scores

`scores.baseline` (migration 0021, jsonb, null): on a row of the layer `corrected`, the raw
layer scored the same way, `{overall, dimensions, n_findings}`.

- `GET /api/series/{id}` and the items of `GET /api/series` get

  ```json
  "corrected_score": {"overall": 94.0, "computed_at": "…", "version": 3,
                      "raw": {"overall": 61.0}}
  ```

  null without one. `latest_score` stays the raw layer's, as do the list's `score_max`
  filter and the overview.
- `GET /api/series/{id}/scores` takes `layer=raw|corrected` (all without it, as before); an
  item gets `baseline` and, for `corrected`, `version`.

### Job

`rescore_corrected(series_id, org_id)`, queue `runs`, no retry; queued when a correction is
approved (run in the request in inline mode).

## Behaviour

1. **Default.** Without `layer`, every route answers exactly as before this spec.
2. **Reading.** A corrected read is `corrections.read_layer`: the raw rows of the window
   with the approved windows laid over. Everything after the read (downsampling, profile,
   alignment, residual, scatter) is unchanged. A cache error is 503 as for raw.
3. **Re-scoring.** After an approval the job takes the series' newest raw score and its run:
   the run's checks (those that look at one series; checks across series are left out and
   named in the log), its window and its `now`. It reads the raw rows and the
   `corrected@latest` rows of that window, runs the checks on each with the series' metadata
   and baseline as a run would, and stores one `scores` row with `layer = 'corrected'`, the
   corrected result, `run_id` of that run, and `baseline` = the raw result; and the
   `score_days` row of the layer `corrected`. No findings or metrics are stored for it.
4. **Nothing to score.** A series that was never scored, a window with more rows than
   `TABAYYUN_CORRECTION_MAX_ROWS`, or a cache error: no row is written, the reason is
   logged, and the approval stands.
5. **Version.** The row's version is the highest approved version when the rows were read
   (`scores.baseline.version`); a later approval queues the job again and its row is newer.
6. **Raw is unchanged.** No raw score, finding or metric is written or changed by this job,
   and the overview, the series list's score and the alerts keep reading the raw layer.
7. **Tenancy and roles** as for the routes today (viewer reads; other workspaces 404).

## Acceptance criteria

- [ ] `layer` is parsed and refused as above on all five routes; without it the answers are
      byte-identical to before.
- [ ] Chart and profile of `corrected@latest` and `corrected@v{n}` show the corrected rows,
      with `layer_version` and an ETag that differs from the raw one.
- [ ] Compare chart, scatter and residual read `corrected@latest` per series.
- [ ] Approving a correction that fills a gap stores a `corrected` score higher than its
      `baseline`, and the series answers `corrected_score` with both numbers.
- [ ] A series without a score, and a window over the row limit, write nothing and the
      approval succeeds.
- [ ] Raw scores, findings and metrics are the same before and after the job.

## Test cases

- **API, no database** (`api/tests/test_layers.py`): `parse_layer_names`, `refused_names`.
- **API, database** (`api/tests/db/test_layer_reads.py`): `chart_reads_a_layer`,
  `profile_reads_a_layer`, `default_is_raw_and_unchanged`, `etag_differs_by_layer`,
  `unknown_version`, `compare_and_residual_read_corrected_latest`,
  `versions_refused_for_several_series`.
- **API, database** (`api/tests/db/test_rescoring.py`): `approval_rescored_raw_to_corrected`,
  `series_shows_both_scores`, `scores_filter_by_layer`, `never_scored_series`,
  `window_too_large`, `raw_scores_untouched`, `second_approval_scores_again`.

## Implementation notes

(filled in when done)

## Out of scope

- Web: the layer switch on the chart and the score pair on the series page: S11-4.
- Findings and metrics of the corrected layer, and a corrected layer in the overview and in
  alerts: after R1; the pair of scores is what S11-3 promises.
- Runs on the corrected layer (a suite that checks corrected data): with RepairFlows, R2.
- Export and publish of a layer: S11-5.
