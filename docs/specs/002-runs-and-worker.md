# Spec 002 — Runs and the worker

Sprint 6, story S6-2. Depends on: 001. Packages: `api/src/tabayyun/jobs/`,
`api/src/tabayyun/routers/runs.py`, `api/src/tabayyun/services/runs.py`, `api/Dockerfile`,
`deploy/` (worker service and CapRover app), `docs/architecture/03-system-architecture.md`.

## Goal

An upload no longer computes in the request. `POST /api/runs` stores the file, creates a
`Run`, enqueues a job and returns immediately; a worker process executes the checks through
the core, persists the results (spec 003 and 004) and marks the run finished; clients poll the
run until it is terminal. The stateless `POST /api/checks/run` stays for ad-hoc use.

## User story

As a data engineer, I upload a 50 MiB CSV, get a run id straight away, and can close the
tab; when I come back the run shows its status, how long it took and its findings.

## Interface

Job queue: Procrastinate (ADR-0004) with the Postgres connector on `DATABASE_URL`; app object
`tabayyun.jobs.app`; task `run_checks_job(run_id: str)` in queue `runs`, `max_attempts=1`
(`retry=False`). Migration `0002` applies Procrastinate's schema so one migrate command
creates the queue tables next to ours.

Settings: `TABAYYUN_INLINE_JOBS` (default `false`): when `true` the API executes the job in
the request's background task instead of enqueueing, for tests and single-process dev.

Routes:

```
POST /api/runs                     multipart: file, series_id, unit, ts_col, value_col,
                                   quality_col, ingest_col, physical_min, physical_max, now_ns
                                   → 202 {"id", "status": "queued", "created_at"}
GET  /api/runs/{id}                → 200 Run
GET  /api/runs?limit=50&cursor=    → 200 {"items": [Run], "next_cursor"}
```

`Run` response:

```json
{"id": "…", "trigger": "upload", "status": "succeeded",
 "window": {"start": 1788220800000000000, "end": 1790019900000000001}, "now_ns": 1790019900000000000,
 "started_at": "…", "finished_at": "…", "duration_ms": 412,
 "stats": {"n_series": 1, "n_samples": 1940, "n_findings": 9, "n_metrics": 34, "skipped": 2},
 "series": [{"id": "…", "external_id": "demo", "score": 98.4}],
 "error": null}
```

Worker process: `python -m tabayyun.jobs` (schema guard, then a Procrastinate worker on the
`runs` and `maintenance` queues) from the api image with `TABAYYUN_ROLE=worker`; compose
service `worker` and CapRover app `tabayyun-worker` with the same environment as the api
app and no HTTP port. Upload bytes live in the `uploads` table so api and worker need no
shared filesystem.

Edited during implementation: the role switch (`TABAYYUN_ROLE`) replaces a separate command
because CapRover apps run the image's CMD; the exact ns window bounds are kept in
`runs.stats.window` next to the `timestamptz` columns, which hold microseconds and would
drop the core's exclusive `+1 ns` end.

## Behaviour

1. `POST /api/runs` validates the form exactly like `/api/checks/run` (size cap 50 MiB, 400
   on empty, 422 on an unparsable CSV: parsing happens in the request so the user learns about
   a bad file immediately, the parsed table is discarded and the raw bytes stored).
2. In one transaction: insert `uploads` (bytes, filename, params), insert `runs` with
   `status = queued`, `trigger = upload`, and enqueue the job with Procrastinate's
   transactional enqueue, so a run row without a job or a job without a row cannot exist.
3. The worker sets `status = running`, `started_at = now()`, loads the upload, parses the CSV,
   resolves source and series (spec 004), calls `core.run_checks` with the stored series
   metadata merged with the upload parameters, persists scores, metrics and findings (spec
   003) in one transaction with the run update, sets `status = succeeded`, `finished_at`,
   `stats` and `window`. In this spec the worker creates the `Uploads` source and the series
   row by external id and persists the score row; metrics and findings follow in spec 003,
   metadata precedence in spec 004.
4. Any exception in the worker marks the run `failed` with `error` set to a one-line message
   (`cannot parse CSV: …`, `core error: …`), never a traceback, and the exception is logged
   with the run id. A crashed worker (job lost) leaves the run `running`; a periodic
   Procrastinate task every 10 minutes marks runs running for more than 30 minutes as
   `failed` with `error = "worker lost"`.
5. `GET /api/runs/{id}` returns 404 for unknown ids and for ids outside the caller's
   workspace (single default workspace until spec 007). `GET /api/runs` is keyset-paginated on
   `(created_at desc, id)`, `limit` 1–200.
6. Structured log lines: `run.queued`, `run.started`, `run.finished` (with `duration_ms`,
   `n_findings`), `run.failed`, all carrying `run_id`.
7. `/healthz` gains `queue: {"pending": n, "running": n}` from the Procrastinate tables.

## Acceptance criteria

- [x] `POST /api/runs` returns 202 with an id within 200 ms for a 50 MiB file (parsing only).
- [x] With a worker running, the run reaches `succeeded` and `GET /api/runs/{id}` shows
      stats matching what `POST /api/checks/run` returns for the same file.
- [x] With `INLINE_JOBS=true` the same passes without a worker (CI path).
- [x] A CSV with a broken value column yields `failed` with a readable `error`; the API never
      returns a traceback.
- [x] Killing the worker mid-run leaves the run `running`; after the reaper task it is
      `failed` with `worker lost`.
- [ ] The worker image starts with `python -m tabayyun.jobs` and processes a run on CapRover.
      (Needs the `tabayyun-worker` app and its token; checked after merge, ticked in the
      spec 003 PR.)
- [x] The uploads table row is deleted when the run reaches a terminal state (the CSV is not
      kept; the cache in spec 006 takes over) and the run keeps its stats.

## Test cases

Unit (`api/tests/test_runs_api.py`, inline jobs, database fixture from spec 001):
- `test_post_run_returns_202_and_queued`.
- `test_run_succeeds_inline_and_matches_stateless_endpoint`.
- `test_run_failed_on_core_error_has_message_not_traceback` (a broken file is a 422 in the
  request, so the failed-run path is exercised with a core error) and
  `test_unparsable_csv_is_422_in_the_request`.
- `test_list_runs_pagination`.
- `test_get_run_unknown_404`.
- `test_upload_deleted_after_terminal_state`, `test_score_row_persisted`,
  `test_enqueue_is_transactional_with_the_run`, `test_healthz_reports_queue_depth`.

Unit (`api/tests/test_jobs.py`): `test_reaper_marks_stale_runs_failed`.

Integration (manual, recorded in the PR): worker container on CapRover processes an upload
from the deployed web app.

## Out of scope

- Scheduled suites and datasets as run inputs (sprint 10).
- Retention of uploads or raw data (spec 006 cache).
- Authentication of the caller (spec 007).
