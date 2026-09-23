# Spec 004 — Series and sources from uploads, metadata API

Sprint 6, story S6-4. Depends on: 001, 002. Packages:
`api/src/tabayyun/services/series.py`, `api/src/tabayyun/routers/series.py`,
`api/src/tabayyun/routers/sources.py`.

## Goal

Every uploaded series becomes a durable `Series` record under an `upload` source, its
metadata (unit, limits, kind, interval) can be edited through the API, and the next run of the
same series uses the stored metadata. Series are the anchor that findings, scores and, from
spec 006, cached data hang from.

## User story

As a data engineer, I set the physical range and unit of a tag once; every later upload of
that tag applies them, and I can list my tags with their latest score.

## Interface

Routes:

```
GET   /api/sources                          → 200 {"items": [Source]}
GET   /api/series?source_id&q&kind&limit&cursor
                                            → 200 {"items": [SeriesSummary], "next_cursor"}
GET   /api/series/{id}                      → 200 Series (metadata + latest score + counts)
PATCH /api/series/{id}                      body: any of name, unit, kind, expected_interval_ns,
                                                  physical_min, physical_max, operational_min,
                                                  operational_max, resolution, non_negative,
                                                  asset_path, metadata
                                            → 200 Series
```

`SeriesSummary`: `id`, `source_id`, `external_id`, `name`, `unit`, `kind`, `latest_score`
(`overall`, `computed_at`) or null, `open_findings` (count), `last_run_at`.

Validation (422 with the field name): `physical_min < physical_max` when both set, same for
operational; operational band inside the physical band when both set; `resolution > 0`;
`expected_interval_ns > 0`; `unit` ≤ 32 chars; `kind` in the four values; `metadata` is a
JSON object ≤ 8 KiB.

## Behaviour

1. Each workspace has exactly one source of type `upload` named `Uploads`, created on first
   use (idempotent, unique on `(workspace_id, name)`).
2. On a run, the worker upserts the series by `(source_id, external_id = series_id form
   field)`. Metadata precedence: upload form fields override stored values *for that run and
   are saved* (a user who passes `physical_max` in the form has stated a fact); fields not in
   the form come from the stored series; fields in neither are auto-derived by the core as
   today. `name` defaults to `external_id`.
3. `PATCH` applies only the fields present in the body (partial update), bumps `updated_at`,
   and returns the full record. Unknown fields are 422.
4. `GET /api/series` searches `q` case-insensitively against `external_id` and `name`;
   keyset pagination on `(name, id)`; `limit` 1–500, default 50.
5. Deleting a series is not offered in this sprint; sources cannot be created through the API
   yet (connectors arrive in sprint 9).

## Acceptance criteria

- [x] Uploading `series_id=demo` twice creates one series row with two runs.
- [x] `PATCH` setting `physical_max = 100` followed by an upload without `physical_max` runs
      `tby.physical_range` with 100 (visible in the run's findings or metrics).
- [x] An upload passing `physical_max = 90` runs with 90 and the stored value becomes 90.
- [x] Invalid combinations (`physical_min ≥ physical_max`, operational band outside physical)
      are 422 naming the field.
- [x] `GET /api/series/{id}` shows the latest score and the open findings count after a run.
- [x] `GET /api/series?q=` matches on name and external id, paginated.
- [x] Epoch-integer timestamps are read in the declared `ts_unit` or the inferred one; a unit
      that yields dates outside 1971–2199 is a 422 with a hint (added, ADR-0014).

## Test cases

Unit (`api/tests/db/test_series_api.py`, database fixture, inline jobs):
- `test_upload_creates_source_and_series_once`.
- `test_patch_partial_update_and_validation` (parametrised over the invalid combinations).
- `test_stored_metadata_used_on_next_run`, `test_form_overrides_and_persists`.
- `test_series_summary_has_latest_score_and_open_findings`.
- `test_series_search_and_pagination`; added `test_patch_partial_update` and
  `test_epoch_seconds_upload_records_the_unit`.

Unit (`api/tests/test_ts_unit.py`, no database): inference for each unit, declared units too
small and too large, pre-1971 integers, text timestamps, thresholds pinned to the CLI source,
and the stateless endpoint.

## Implementation edits

- Added the `ts_unit` upload field (`auto|s|ms|us|ns`) on `POST /api/runs` and
  `POST /api/checks/run`, recorded as `stats.ts_unit` on runs. Reason: a production check
  uploaded epoch seconds, the API read them as nanoseconds and reported a 19,705-day gap.
  Decision and alternatives in ADR-0014.
- Precedence covers the fields the upload form carries (`unit`, `physical_min`,
  `physical_max`); the core receives name, unit, kind, interval, physical limits, resolution
  and non-negativity. The operational band is stored and validated but not sent to the core,
  whose `tby.operational_range` learns its band from the baseline.
- The worker reads the stored series before the core runs and creates or updates it only in
  the completion transaction, so a failed run leaves no series behind. Form limits that
  contradict the stored series are a 422 at request time and fail the run if the series
  changed in between.
- Validation runs on the merged metadata (stored values plus the PATCH), and the 422 names
  the field the request sent. Explicit `null` clears a nullable field; `name`, `kind` and
  `metadata` cannot be null.
- `open_findings` counts `open` and `acked` findings, matching the default findings list.
  `GET /api/sources` also returns `n_series`; `GET /api/series/{id}` also returns `n_runs`.
- The upload-source helpers moved from `services/runs.py` to `services/series.py`.

## Out of scope

- Connector-backed sources and metadata import from PI AF or OPC UA (sprint 9).
- Datasets and related-series suggestions (spec 008, sprint 7).
- Series deletion and merge.
