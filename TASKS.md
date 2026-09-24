# Tabayyun — Backlog

Status legend: `[ ]` open, `[~]` in progress, `[x]` done. One spec per session; mark it here
and add one line per non-obvious decision below the entry (these are the session notes the
next session reads). Sprint priorities, actual dates and the forecast live in
`docs/roadmap/sprints.md`; the story ids there (`S6-1`) map to specs here.

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

## Sprint 5 — landing and planning (done 21 – 22 Sep 2026)

- [x] S5-1 web image copies `pnpm-workspace.yaml`, doc corrections (PR #16, with the Node 22
      pin).
- [x] S5-2 sprint plan merged and linked (PR #15); spec-driven workflow, specs 001–005,
      this backlog (PR #22).
- [ ] S5-3 owner: confirm the api app's session secret is generated, delete merged branches.
- [x] S5-4 upload page exposes `physical_min` / `physical_max`.
      - Implemented on the existing page rather than waiting for spec 005's `/runs/new`,
        because the API already accepts the fields; spec 005 keeps them when the form moves.
      - Empty inputs are dropped before submit: FastAPI parses `physical_min=""` as a 422.

## Sprint 6 — persistence and jobs (22 – 23 Sep 2026, planned 27 Sep – 3 Oct)

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
- [x] **005 Runs list and report in the web app** — `docs/specs/005-runs-web.md`
      `/runs`, `/runs/new` (with physical limits), `/runs/:id` with polling and evidence.
      - Runs keep `stats.skipped_checks`; score tiles read the run's score row; the series
        link is a metadata panel until the series page (sprint 9).
      - One findings list (buttons in a grid) serves desktop and phone widths.
      - Vitest pinned to `TZ=Asia/Riyadh` to cover the UTC offset; form tests submit
        directly because jsdom ignores user-event files for `required`.
      - Deployed round trip verified 2026-09-23 after PR #29 (series `e2e-web`, findings
        resolved as "test upload").
      - Review round added `?run_id=` on `GET /api/series/{id}/scores`, findings paging in the
        report, plain-text error bodies and a score-row retry.
      - Follow-up (core): `tby.physical_range` reports each excursion as its own finding; a
        limit set inside the normal range gave 34 findings in the local check. ADR-0011's
        episode rule should apply to it as it does to spikes.

## Sprint 7 — Parquet cache and cross-series checks (started 23 Sep 2026)

Branch `sprint/07-cache-cross-series`, one PR per spec, in this order (must-haves first).
Sprint decisions (23 Sep): cache on S3-compatible storage now (RustFS 1.0 live, in dev and
in CI; it replaced an unmaintained MinIO the same day); cross-series checks
reach the API through series groups and dataset runs (S7-8 raised to should); checks stay
Rust kernels, no Polars in the core (ADR-0015 supersedes ADR-0002).
- [x] **006 Parquet cache on local disk or S3, coverage** — `docs/specs/006-parquet-cache.md` (S7-1, S7-2)
      - Store is RustFS 1.0.0 live, in dev and in CI (the unmaintained MinIO was replaced the
        same day; the bucket was still empty).
      - The cache write runs after the completion transaction (no network I/O under the
        series lock); coverage and `stats.cache` follow in a short transaction.
      - Cache keys are series and source UUIDs; object_store 0.14.2 (0.13's quick-xml failed
        `cargo audit`), independent of parquet's version.
      - Done 2026-09-23: CI runs the S3 tests against RustFS; a live two-month upload wrote
        2 objects and its coverage row.
- [x] **008 Multi-series checks, series groups and dataset runs** — `docs/specs/008-multi-series-and-datasets.md` (S7-3, S7-8)
      - `run_multi` takes each series' profile so a dataset run finds what an upload of the
        same data finds (the checks' adaptive thresholds depend on it).
      - Cross checks are injected (`run_multi_with`); the built-in list stays empty until
        009–011, so dataset runs today run the single-series checks and report groups.
      - Groups with a member outside the dataset are reported in `groups_skipped`, not dropped.
      - `POST /api/runs` JSON answers with the upload's `{id, status, created_at}` shape.
      - Cross-series findings merge only within their group (ADR-0013 amendment).
      - Live dataset run verified after PR #32; the cross-series finding after PR #33 (009).
- [x] **009 `tby.correlation_break`** — `docs/specs/009-correlation-break.md` (S7-4)
      - Pair findings attach to the pair's first member with `partner`; metrics carry the
        partner in their name; dedup also matches `partner` (ADR-0013 amendment).
      - Lag is read from first differences with the pair's sign, and judged only where the
        correlation held and the reference lag is stable (both found on 3W WELL-00019).
      - 3W: dead PDG gauges on WELL-00001 skip; WELL-00019 (hydrate) keeps ρ ≈ −1 through the
        event, one short unexplained dip flagged. Follow-up: the CLI cannot read brotli Parquet.
- [x] **010 `tby.redundant_disagreement`** — `docs/specs/010-redundant-disagreement.md` (S7-5)
      - Metric carries the group (`max_abs_diff:<group>`) so a series in two groups keeps both.
      - Auto tolerance for 3+ members from the MAD of deviations from the bin median, floored
        at 2 × the data's estimated resolution when metadata has none.
      - Short runs are dropped before close runs merge (spec order), so blips stay silent.
- [x] **011 `tby.balance_residual`** — `docs/specs/011-balance-residual.md` (S7-6)
      - Flag rule changed: r outside the loss band by more than k σ_r (the spec's raw |r| test
        flagged normal losses); `reason` is `above_band` or `below_band`.
      - Suspect changed: common mode of all shares removed before attributing (the spec's rule
        gave the opposite side half the blame); needs a true median, not `median_mad`'s rank.
      - Episode building and summary formatting now shared in `cross.rs` with spec 010.
- [x] **012 Seasonality in the profile, `tby.seasonality_break`** — `docs/specs/012-seasonality-break.md` (S7-7)
      - ACF after removing the moving-average trend (raw ACF read random walks and wind as daily).
      - Shortest significant candidate, not the highest ACF (harmonics tie); load reads daily.
      - Reference always from the leading segments: the run's profile is its own data.
      - OPSD DE load/wind/solar 2015–2020: silent; a planted flat load week is the one finding.
      - Grid capped at 500 000 bins with checked span arithmetic (a stray far timestamp).
        Follow-up: `frame::modal_interval` (`0 − i64::MIN`) and `Profile::compute`'s
        `3 × interval` gap cut still overflow on timestamps near the i64 limits.
- [ ] **016 `tby.physical_range` episodes** — `docs/specs/016-physical-range-episodes.md` (S7-9, could)
- [ ] **017 CLI epoch units match the API** — `docs/specs/017-cli-epoch-units.md` (S7-10, could)

## Sprint 8 — login, tenants and RBAC (forecast end 29 Sep – 1 Oct)

- [ ] 007 Row-level security, memberships and roles (ADR-0007)
- [ ] 013 Keycloak realm and OIDC BFF
- [ ] 014 Tenant APIs and admin panel v1
- [ ] 015 Security baseline pass 1

Owner prerequisites before sprint 8 starts (Google OAuth client, Keycloak app, SMTP):
`docs/roadmap/sprints.md`, "Owner and external dependencies".
- [x] Keycloak at miftachun.apps.data-and-ai-dude.ch: named admin, OTP and brute-force
      protection in the master realm (23 Sep).
- [x] Google OAuth client (External, redirect `…/realms/tabayyun/broker/google/endpoint`);
      credentials in the owner's password manager, to CapRover env vars in S8-1 (23 Sep).
- [x] SMTP through the Google Workspace relay (`smtp-relay.gmail.com:587`, allowed by the
      server IP); SPF, DKIM and DMARC fixed on data-and-ai-dude.com and .ch (23 Sep).
- [ ] Around 7 Oct: move both DMARC records from `p=none` to `p=quarantine` once the
      reports show only Google sending (reminder scheduled).

## Later sprints

Stories S9-1 onwards in `docs/roadmap/sprints.md` become specs 018+ when their sprint
starts; the plan is the backlog until then.

- [ ] **018 Compare view** — `docs/specs/018-compare-view.md` (S9-7), drafted early on request
      (23 Sep) with `docs/research/06-multi-series-comparison.md`; needs S9-5's chart endpoint.
      The other sprint 9 stories take 019+. Approved 24 Sep; scatter stays in sprint 10 (S10-7).
