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
GET   /api/series/{id}/metrics?name&since&until&limit  → 200 {"items": [{"ts","value","run_id","check_id","name"}]}
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
   `series_id` and `check_id`, `status in (open, acked)`, the same evidence shape (set of
   top-level evidence keys), created by an earlier run, and `window` overlapping the incoming
   window; among several, the largest overlap ratio (intersection over union) wins. If found,
   update it: `window = union`, `severity`, `score_impact`, `summary`, `evidence` from the
   incoming finding, `last_run_id = run`, `occurrences += 1` once per run,
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

- [x] Uploading the synthetic faulty series twice yields the same number of findings as once,
      each with `occurrences = 2` and `last_run_id` pointing at the second run.
- [x] Uploading a second file whose gap window overlaps the first file's gap merges into one
      finding whose window is the union.
- [x] A finding `resolved` and then re-detected appears as a new finding.
- [x] `PATCH` with `muted` and no reason is 422; `muted → resolved` is 409.
- [x] The default list excludes `muted` and `resolved`; `status=all` includes them.
- [x] Filters and pagination return stable, non-overlapping pages ordered by window start.
- [x] Metrics and scores endpoints return what the run persisted, newest first.
- [x] ADR-0013 is merged and the catalogue's "Evidence" bullets link to it.

## Test cases

Unit (`api/tests/db/test_findings_service.py`, database fixture):
- `test_insert_new_findings`, `test_merge_overlapping_open_finding_unions_window`,
  `test_acked_stays_acked_on_merge`, `test_resolved_not_merged_new_finding_created`,
  `test_two_incoming_merge_into_one_existing`; added `test_merge_growing_backwards_keeps_the_id`,
  `test_other_evidence_shape_is_not_absorbed`, `test_best_overlap_wins_among_candidates`,
  `test_findings_of_one_run_do_not_merge_with_each_other`, `test_other_check_is_not_absorbed`,
  `test_metrics_rewritten_by_a_rerun`.

Unit (`api/tests/db/test_findings_api.py`, inline jobs):
- `test_list_defaults_to_open_and_acked`, `test_filters_and_cursor_pagination`,
  `test_status_transitions_table` (parametrised over the allowed and forbidden pairs),
  `test_mute_requires_reason`, `test_metrics_and_scores_endpoints`; the acceptance criteria
  are `test_same_upload_twice_keeps_one_finding_per_problem`,
  `test_overlapping_gap_in_a_second_file_merges_into_the_union` and
  `test_resolved_then_redetected_is_a_new_finding`.

## Implementation edits

- Dedup matches on evidence shape as well (behaviour 2): the synthetic faulty series showed
  `tby.completeness` emitting each gap plus a whole-window finding spanning them, and "any
  overlapping finding of the same check" folded the gaps into it. Findings of the same run
  never merge with each other, and `occurrences` counts runs, so two incoming findings of one
  run merging into one existing finding add one occurrence. Recorded in ADR-0013.
- Metric items also carry `check_id` and `name`, since without the `name` filter the list
  mixes metrics. `limit` is 1–1000 (default 100) for metrics and 1–500 (default 50) for scores.
- `run_id` filters on `first_run_id` or `last_run_id`; the schema keeps no per-run link.
- Stored window ends round up to the microsecond so the stored window contains the core's.
- The database tests live in `api/tests/db/` next to the fixture of spec 001.

## Out of scope

- Auto-resolution when a later run covering the window no longer reports the problem
  (sprint 10, with suites and the findings inbox).
- Assignment, comments and false-positive feedback (sprint 10).
- Row-level security (spec 007).
