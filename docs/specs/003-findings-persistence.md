# Spec 003 — Findings persistence, deduplication and API

Sprint 6, story S6-3 and S6-6. Depends on: 001, 002. Packages:
`api/src/tabayyun/services/findings.py`, `api/src/tabayyun/routers/findings.py`,
`docs/adr/0013-run-and-finding-lifecycle.md`.

## Goal

Findings, metrics and scores from every run are stored; a finding that a later run reports
again for the same series, check and overlapping window updates the existing open finding
instead of creating a duplicate; findings can be listed with filters and their status can be
changed. ADR-0013 records the lifecycle.

## User story

As a data engineer, I upload the same month twice and see one finding per real problem, with
"seen in 2 runs", not two copies; I acknowledge the ones I know about and they stop showing
in my open list.

## Interface

Routes:

```
GET   /api/findings?series_id&check_id&severity&dimension&status&run_id&since&until&limit&cursor
      → 200 {"items": [Finding], "next_cursor"}
GET   /api/findings/{id}          → 200 Finding
PATCH /api/findings/{id}          body {"status": "acked|muted|resolved|open", "reason": "…"}
      → 200 Finding
GET   /api/series/{id}/metrics?name&since&until&limit  → 200 {"items": [{"ts","value","run_id"}]}
GET   /api/series/{id}/scores?limit                    → 200 {"items": [Score]}
```

`Finding` response: the core fields (`check_id`, `series_id`, `dimension`, `severity`,
`window {start,end}` in ns, `score_impact`, `summary`, `evidence`) plus `id`, `status`,
`status_reason`, `status_at`, `first_run_id`, `last_run_id`, `occurrences`, `created_at`,
`updated_at`.

Filters: `severity` and `status` accept comma lists; `since`/`until` are RFC 3339 or epoch ns
and select findings whose window overlaps `[since, until)`; default sort `window_start desc,
id`; `limit` 1–500, default 50; keyset cursor.

## Behaviour

1. Persisting a run's report happens in the run's transaction (spec 002): scores row per
   series (`layer = raw`), one metrics row per core metric, findings through the dedup rule
   below. A run that fails persists nothing.
2. Dedup rule: for each incoming finding, look for an existing finding with the same
   `series_id` and `check_id`, `status in (open, acked)` and `window` overlapping the incoming
   window. If found, update it: `window = union`, `severity`, `score_impact`, `summary`,
   `evidence` from the incoming finding, `last_run_id = run`, `occurrences += 1`,
   `updated_at = now()`; an `acked` finding stays `acked`. If not found, insert with
   `first_run_id = last_run_id = run`, `status = open`. Two incoming findings that overlap the
   same existing one both merge into it. `muted` and `resolved` findings never absorb new
   ones: a re-reported problem after resolution is a new finding.
3. Status transitions: `open → acked | muted | resolved`, `acked → resolved | open`,
   `muted → open`, `resolved → open`. `muted` requires a non-empty `reason` (max 500 chars);
   others accept it optionally. Any other transition is 409 with the current status in the
   body. `status_at` is set on every change; `status_by` stays null until spec 007.
4. `GET /api/findings` without `status` returns `open` and `acked` only; `status=all`
   returns everything.
5. Evidence is returned as stored JSON; summaries are the core's plain-language sentences,
   never rewritten by the API.
6. Deleting is not offered; a wrong finding is `resolved` with a reason.

## Acceptance criteria

- [ ] Uploading the synthetic faulty series twice yields the same number of findings as once,
      each with `occurrences = 2` and `last_run_id` pointing at the second run.
- [ ] Uploading a second file whose gap window overlaps the first file's gap merges into one
      finding whose window is the union.
- [ ] A finding `resolved` and then re-detected appears as a new finding.
- [ ] `PATCH` with `muted` and no reason is 422; `muted → resolved` is 409.
- [ ] The default list excludes `muted` and `resolved`; `status=all` includes them.
- [ ] Filters and pagination return stable, non-overlapping pages ordered by window start.
- [ ] Metrics and scores endpoints return what the run persisted, newest first.
- [ ] ADR-0013 is merged and the catalogue's "Evidence" bullets link to it.

## Test cases

Unit (`api/tests/test_findings_service.py`, database fixture):
- `test_insert_new_findings`, `test_merge_overlapping_open_finding_unions_window`,
  `test_acked_stays_acked_on_merge`, `test_resolved_not_merged_new_finding_created`,
  `test_two_incoming_merge_into_one_existing`.

Unit (`api/tests/test_findings_api.py`):
- `test_list_defaults_to_open_and_acked`, `test_filters_and_cursor_pagination`,
  `test_status_transitions_table` (parametrised over the allowed and forbidden pairs),
  `test_mute_requires_reason`, `test_metrics_and_scores_endpoints`.

## Out of scope

- Auto-resolution when a later run covering the window no longer reports the problem
  (sprint 10, with suites and the findings inbox).
- Assignment, comments and false-positive feedback (sprint 10).
- Row-level security (spec 007).
