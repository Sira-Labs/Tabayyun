# Tabayyun — Backlog

Status legend: `[ ]` open, `[~]` in progress, `[x]` done. One spec per session; mark it here
and add one line per non-obvious decision below the entry (these are the session notes the
next session reads). Sprint dates and priorities live in `docs/roadmap/sprints.md`; the
story ids there (`S6-1`) map to specs here.

## Sprints 1–5 (done before specs existed)

- [x] Research 01–05, vision, domain model, architecture, authz design, frontend design,
      security baseline, roadmap, ADRs 0001–0012, sprint plan.
- [x] Rust core: 20 of 30 checks, profile, scoring v2, downsampling, synth, CLI, PyO3 wheel.
- [x] API: FastAPI skeleton, `POST /api/checks/run` on an uploaded CSV, prod secret guard.
- [x] Web: upload page with score tiles and findings.
- [x] CI (rust core, python bindings, python api, web), release images to GHCR, CapRover
      deploy job, live at the Hetzner CapRover instance.
- [x] Finding aggregation into episodes (ADR-0011), frozen-tag rule, historian timestamps,
      test datasets (UCI, OPSD, Petrobras 3W) with measured expectations.
      - Sprint 5 lesson: a Dependabot Node major bump removed Corepack from the web image;
        Node majors are now ignored by Dependabot and taken by hand on the LTS line.

## Sprint 5 — landing and planning (20 – 26 Sep 2026)

- [x] S5-1 web image copies `pnpm-workspace.yaml`, doc corrections (PR #16, with the Node 22
      pin).
- [x] S5-2 sprint plan merged and linked (PR #15); spec-driven workflow, specs 001–005,
      this backlog (PR #22).
- [ ] S5-3 owner: confirm the api app's session secret is generated, delete merged branches.
- [x] S5-4 upload page exposes `physical_min` / `physical_max`.
      - Implemented on the existing page rather than waiting for spec 005's `/runs/new`,
        because the API already accepts the fields; spec 005 keeps them when the form moves.
      - Empty inputs are dropped before submit: FastAPI parses `physical_min=""` as a 422.

## Sprint 6 — persistence and jobs (27 Sep – 3 Oct 2026)

- [x] **001 Persistence schema and migrations** — `docs/specs/001-persistence-schema.md`
      SQLAlchemy 2 async models, Alembic, hypertables with the Apache-2-only switch, seed
      org/workspace, schema-revision guard at startup, CI Postgres service.
      - Migrations are packaged (`tabayyun/db/migrations`) and the image runs
        `python -m tabayyun.db.migrate upgrade head` on start; `api/alembic.ini` is only the
        developer CLI entry. The wheel has no checkout to read `api/alembic/` from.
      - Enumerated columns are text + named CHECK constraints, not Postgres enums: a later
        spec extends the values with one `ALTER TABLE`, and autogenerate stays clean.
      - Descending indexes are declared as `text("col DESC")` expressions in models and
        migration alike; Alembic reflects them as expressions, and `postgresql_ops` diffs.
      - Hypertables are created with `create_default_indexes => false` so the schema is
        identical in `auto`, `on` and `off` mode and `alembic check` passes in every mode.
      - Window bounds (`findings`, `runs`, `coverage`, `metrics.ts`) are `timestamptz`; core
        findings carry ns since epoch and spec 003 converts at the persistence boundary.
      - The schema guard runs in the lifespan, not in `create_app()`: an unreachable
        database degrades `/healthz` instead of crashing, a revision mismatch exits 3.
      - Deployed-app criterion verified 2026-09-22 after PR #25 (`schema_revision` 0001).
- [x] **002 Runs and the worker** — `docs/specs/002-runs-and-worker.md`
      `POST /api/runs` + Procrastinate worker, uploads in Postgres, inline-jobs mode for
      tests, stale-run reaper, worker container and CapRover app.
      - Transactional enqueue calls Procrastinate's `procrastinate_defer_jobs_v1` on the
        run's own connection; the `_v1` SQL functions are Procrastinate's stable interface,
        so the API needs no open Procrastinate app.
      - Migration 0002 executes Procrastinate's `schema.sql` through the raw driver cursor
        (it contains `%` and dollar-quoted bodies); a Procrastinate upgrade becomes a new
        revision that runs its own migration files. Downgrade drops every `procrastinate_*`
        table, function and type dynamically.
      - One image, `TABAYYUN_ROLE=api|worker`: CapRover apps run the image's CMD, so a
        separate command is impractical. The worker guards the schema and exits 3 until
        the api has migrated; the health check is an entrypoint subcommand.
      - Inline mode commits explicitly before scheduling the background task: FastAPI runs
        background tasks before the yield-dependency teardown, so the worker would not see
        the run otherwise.
      - `runs.stats.window` keeps the exact ns bounds; the `timestamptz` columns truncate to
        microseconds and lose the core's exclusive `+1 ns` end.
      - The worker creates the `Uploads` source and the series row (ON CONFLICT DO NOTHING)
        and persists the score row now; findings and metrics (003) and metadata precedence
        (004) follow. `retry=False` is Procrastinate's `max_attempts=1`.
      - CapRover worker verified 2026-09-22 (release run 26): a run posted to the live API
        went from `queued` to `succeeded`.
- [x] **003 Findings persistence, dedup and API** — `docs/specs/003-findings-persistence.md`
      Persist scores/metrics/findings, overlap dedup with occurrences, status transitions,
      list/filter/paginate, ADR-0013 lifecycle.
      - Dedup also requires the same evidence shape (top-level evidence keys) and a finding
        created by an earlier run; the best overlap ratio wins. `tby.completeness` emits gaps
        plus a whole-window finding over them, which a plain overlap rule would merge.
      - `occurrences` counts runs, not merges; run stats add `n_findings_new` and
        `n_findings_merged`.
      - A union that moves `window_start` earlier is delete + insert under the same id:
        the column is in the primary key and is the hypertable's partition column.
      - A transaction advisory lock per series serialises dedup of concurrent runs.
      - Stored windows round the start down and the end up to the microsecond.
      - `run_id` filters on first or last run; ns and cursor helpers moved to
        `services/timeconv.py` and `services/pagination.py` (runs reuse them).
- [x] **004 Series and sources from uploads** — `docs/specs/004-series-and-sources.md`
      `Uploads` source, series upsert, metadata precedence, `PATCH /api/series/{id}`.
      - Added `ts_unit` for epoch-integer timestamps (ADR-0014): declared or inferred with
        the CLI's thresholds, instants outside 1971–2199 are a 422, runs record the unit.
        Found by the production end-to-end check of spec 003.
      - The worker reads stored metadata before the core and upserts the series only on
        success; form limits are validated against the stored series at request time too.
      - PATCH validates the merged metadata and blames the field the request sent.
      - Operational limits are stored, not sent to the core (it learns the band).
      - `open_findings` = open + acked, matching the default findings list.
      - Follow-ups: CLI per-column inference and range check; `ts_unit` select in the web
        form with spec 005.
- [~] **005 Runs list and report in the web app** — `docs/specs/005-runs-web.md`
      `/runs`, `/runs/new` (with physical limits), `/runs/:id` with polling and evidence.
      - Runs keep `stats.skipped_checks`; score tiles read the run's score row; the series
        link is a metadata panel until the series page (sprint 9).
      - One findings list (buttons in a grid) serves desktop and phone widths.
      - Vitest pinned to `TZ=Asia/Riyadh` to cover the UTC offset; form tests submit
        directly because jsdom ignores user-event files for `required`.
      - Stays `[~]` until the round trip is checked against the deployed worker.
      - Follow-up (core): `tby.physical_range` reports each excursion as its own finding; a
        limit set inside the normal range gave 34 findings in the local check. ADR-0011's
        episode rule should apply to it as it does to spikes.

## Sprint 7 — Parquet cache and cross-series checks (4 – 10 Oct)

Specs to write at sprint start (stories S7-1 … S7-8 in `docs/roadmap/sprints.md`; 007 is
taken by the RLS spec that specs 001–004 already reference):
- [ ] 006 Parquet cache and coverage
- [ ] 008 Multi-series core API and datasets
- [ ] 009 `tby.correlation_break`
- [ ] 010 `tby.redundant_disagreement`
- [ ] 011 `tby.balance_residual`
- [ ] 012 `tby.seasonality_break`

## Sprint 8 — login, tenants and RBAC (11 – 17 Oct)

- [ ] 007 Row-level security, memberships and roles (ADR-0007)
- [ ] 013 Keycloak realm and OIDC BFF
- [ ] 014 Tenant APIs and admin panel v1
- [ ] 015 Security baseline pass 1

## Later sprints

Stories S9-1 onwards in `docs/roadmap/sprints.md` become specs 016+ when their sprint
starts; the plan is the backlog until then.
