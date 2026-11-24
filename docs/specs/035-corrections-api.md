# Spec 035 — Corrections API: propose, compute, approve, reject

Sprint 11, story S11-2. Depends on:
- spec 034 (`tabayyun_core::repair`, its operations, lineage and limits);
- spec 006 (the Parquet cache and its layers), spec 025 (`coverage` and the row estimate);
- spec 007 (workspace roles), spec 014 (audit events), spec 002 (jobs, inline mode);
- ADR-0010 (corrections are versioned layers; raw is immutable).

Packages: `core/tabayyun-core` (layer names, lineage ranges), `core/tabayyun-py` (the `repair`
binding), `api/src/tabayyun/` (tables, service, routes, job), `docs/`.

## Goal

An editor proposes a correction for a window of a series: one repair operation with its
parameters and a reason. The worker computes it on top of the series' current corrected data
and stores the result as the next corrected-layer version in the cache, with what it changed
recorded in Postgres. An editor then approves or rejects it. The raw layer is never written.
Approved versions, in order, are the series' corrected layer.

## User story

As a metering analyst, I propose "fill the gap of last Tuesday by linear interpolation,
because the meter's modem was down", see that 12 samples were added and none could not be
estimated, and approve it. My colleague can read who changed what, when and why, and the
original measurement is still there.

## Interface

### Core

- `Cache::write` and `Cache::read` accept the layer `corrected/v{n}` (n ≥ 1, no leading
  zero) besides one-segment layers such as `raw`. Any other layer with a `/` is refused.
- `Lineage::ranges(frame_ts, max) -> Vec<LineageRange>`: the lineage as runs of touched
  timestamps. Two consecutive lineage timestamps belong to one range unless a timestamp of
  `frame_ts` (the repaired frame's, sorted) lies strictly between them, that is, unless an
  untouched sample separates them. `LineageRange {start_ns, end_ns, changed, added,
  removed}`; `end_ns` is the last timestamp plus one. With more than `max` ranges,
  neighbouring ranges are joined, evenly, until at most `max` remain; counts add up.

### Binding

`tabayyun_core.repair(table, meta, start_ns, end_ns, op, ts_col="ts", value_col="value",
quality_col=None, max_ranges=1000)` returns

```
{method, unrepaired, changed, added, removed, rows,
 ranges: [{start_ns, end_ns, changed, added, removed}],
 window: pyarrow.RecordBatch}   # ts, value, quality of the repaired frame inside [start_ns, end_ns)
```

`op` is the JSON form of spec 034 (`{"method": "impute.linear", "max_gap_ns": …}`). Rows are
sorted by time first, keeping samples that share a timestamp. A refused operation or
argument is `ValueError` with the core's message.

### Tables (migration 0020)

`corrections` (tenant table, row-level security by org like every other):

| Column | Type | |
|---|---|---|
| `id` | uuid | primary key |
| `org_id`, `workspace_id` | uuid | |
| `series_id` | uuid | → `series.id`, cascade |
| `version` | integer | ≥ 1; unique with `series_id`; allocated when proposed |
| `status` | text | `proposed`, `computed`, `failed`, `approved`, `rejected` |
| `method` | text | one of spec 034's seven |
| `params` | jsonb | the operation's parameters without `method` |
| `window_start_ns`, `window_end_ns` | bigint | half-open, ns since the epoch |
| `reason` | text | 1 to 2000 characters |
| `finding_id` | uuid, null | the finding that motivated it; no foreign key (hypertable) |
| `base_version` | integer, null | highest approved version when it was computed; null = raw |
| `stats` | jsonb | `{rows, changed, added, removed, unrepaired}` once computed |
| `error` | text, null | one line, when `failed` |
| `proposed_by`, `decided_by` | uuid, null | → `users.id`, set null |
| `decision_note` | text, null | |
| `created_at`, `computed_at`, `decided_at` | timestamptz | |

`corrected_ranges`: `id`, `org_id`, `workspace_id`, `correction_id` (cascade), `range_start_ns`,
`range_end_ns`, `changed`, `added`, `removed`; index on `(correction_id, range_start_ns)`.
The values before are the base layer's in that range, the values after are the version's in
the cache: the range refers to both, it does not copy them.

### API

All under the workspace of the series; ids that are not UUIDs or not visible are 404.

- `POST /api/series/{series_id}/corrections` (editor) with
  `{"method", "params": {}, "from": "<ns>", "to": "<ns>", "reason", "finding_id": null}`
  → 201, the correction (below), status `proposed`; the job `apply_repair` is queued (run
  in the request in inline mode, so the answer is then already `computed` or `failed`).
- `GET /api/series/{series_id}/corrections?status=&limit=50&cursor=` (viewer) →
  `{"items": [correction…], "next_cursor"}`, newest version first.
- `GET /api/corrections/{id}` (viewer) → the correction with `ranges`.
- `POST /api/corrections/{id}/approve` and `/reject` (editor), body `{"note": null}` →
  200, the correction.

A correction:

```json
{
  "id": "…", "series_id": "…", "version": 3, "status": "computed",
  "method": "impute.linear", "params": {"max_gap_ns": "3600000000000"},
  "window": {"from": "…", "to": "…"}, "reason": "modem down", "finding_id": null,
  "base_version": 2,
  "stats": {"rows": 96, "changed": 0, "added": 12, "removed": 0, "unrepaired": 0},
  "error": null,
  "proposed_by": {"id": "…", "name": "…"}, "decided_by": null, "decision_note": null,
  "created_at": "…", "computed_at": "…", "decided_at": null,
  "ranges": [{"from": "…", "to": "…", "changed": 0, "added": 12, "removed": 0}]
}
```

ns values are strings, as everywhere (`params` keys ending in `_ns` too, and they are
accepted as strings or integers). `ranges` is only in the single-correction answer.

### Settings

`TABAYYUN_CORRECTION_MAX_ROWS` (default 5 000 000): rows one correction may read.

## Behaviour

1. **Propose.** `method` and `params` are validated by the core (a dry call on an empty
   frame): unknown method, unknown or bad parameter → 422 with the core's message. `from <
   to`, both ns; `reason` 1 to 2000 characters after trimming; otherwise 422. `finding_id`,
   when given, must be a finding about this series, otherwise 422 `finding_not_found`. The version is the series' highest so far plus one, under a lock on
   the series row, so two proposals never share one.
2. **Compute** (`apply_repair`, queue `runs`, no retry). The base is the series' corrected
   layer at its highest approved version (raw without one), read over the window widened by
   what the operation looks at: `impute.linear` `max_gap_ns` on each side, or one day
   without it; `impute.like_day` `days` reference days (weeks with `same_weekday`) plus one
   interval; the others nothing. The series' metadata (interval, physical limits) goes with
   it. More rows than `TABAYYUN_CORRECTION_MAX_ROWS` (estimated from coverage before the
   read) → `failed`, "window too large". The core's `repair` runs on the base with the
   window; the repaired rows inside the window are written to `corrected/v{version}`, then
   `stats`, `base_version`, the ranges and `computed` are stored in one transaction. Any
   error of the core or the cache → `failed` with its first line. A correction that is not
   `proposed` when the job starts is left alone.
3. **The corrected layer.** `corrected@v{n}` over `[a, b)` is the raw rows of `[a, b)` in
   which, for every approved correction of version ≤ n in ascending order, the rows inside
   its window are replaced by the rows of its version in the cache. `corrected@latest` is
   that for the highest approved version. (This spec uses it as the base of a computation;
   reading it through the API is spec 036.)
4. **Approve.** Only `computed` → `approved`; anything else is 409 `not_computed`. It is 409
   `stale` when a correction of the same series whose window overlaps was approved after
   this one was computed: its rows were built without that one; propose it again.
   Approved corrections are therefore consistent in version order.
5. **Reject.** `proposed` or `computed` → `rejected`; others 409 `already_decided`. A
   rejected version stays in the cache and in the list and is never part of the layer.
6. **Audit.** `correction.proposed`, `correction.approved`, `correction.rejected` are audit
   events with the actor, series, version and method.
7. **Raw.** Nothing here writes the layer `raw` or the `coverage` of it.
8. **Tenancy.** Corrections of another workspace or org are unknown (404); a viewer who
   proposes or decides gets 403.

## Acceptance criteria

- [ ] The cache accepts `corrected/v{n}` and still refuses other layers with a slash.
- [ ] `Lineage::ranges` splits at untouched samples and joins down to `max`.
- [ ] `tabayyun_core.repair` returns the window's rows, the counts and the ranges.
- [ ] Proposing validates through the core and allocates versions without gaps or doubles.
- [ ] Computing writes `corrected/v{n}` and the ranges; raw rows and raw coverage are the
      same before and after.
- [ ] A second correction is computed on top of the first approved one.
- [ ] Approve, reject, `stale`, `not_computed`, `already_decided`, roles and tenancy behave
      as above, and the three audit events are written.

## Test cases

- **Core** (`cache.rs`): `corrected_version_layers`. (`repair/tests.rs`):
  `ranges_split_at_untouched_samples`, `ranges_join_down_to_the_limit`.
- **Bindings**: `test_repair_window_and_ranges`, `test_repair_refuses_a_bad_operation`.
- **API, no database** (`api/tests/test_corrections_service.py`): `overlay_replaces_window_rows`,
  `context_margin_per_operation`, `params_ns_as_strings`.
- **API, database** (`api/tests/db/test_corrections_api.py`): `propose_compute_approve`,
  `raw_is_untouched`, `second_correction_builds_on_the_first`, `rejected_is_not_in_the_layer`,
  `stale_approval`, `validation`, `window_too_large`, `roles_and_tenancy`, `audit_events`,
  `versions_are_allocated_once`.

## Implementation notes

(filled in when done)

## Out of scope

- Reading a layer through the chart, export and compare endpoints (`layer=`), and re-scoring
  the corrected layer: S11-3 (spec 036).
- Web (window on the chart, preview, approval per workspace policy): S11-4.
- Publishing (`published` status, targets): S11-5. Feedback into thresholds: S11-6.
- Retention of old versions (keep the latest N) and deleting a rejected version's files:
  with S11-5, when publishing defines what must be kept.
- Dual approval and auto-approval: RepairFlows, R2.
