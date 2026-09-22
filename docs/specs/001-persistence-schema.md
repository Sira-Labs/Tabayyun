# Spec 001 — Persistence schema and migrations

Sprint 6, story S6-1. Depends on: nothing (ADR-0003, ADR-0004, ADR-0007). Packages:
`api/src/tabayyun/db/` (engine, session, models), `api/alembic/` (migrations),
`api/src/tabayyun/settings.py`, `.github/workflows/ci.yml`, `deploy/`.

## Goal

The API owns a PostgreSQL schema that holds tenants, sources, series, datasets, runs,
findings, metrics, scores and cache coverage. `alembic upgrade head` creates it from nothing
on a plain Postgres 17 and on TimescaleDB, the three append-heavy tables become hypertables
when TimescaleDB is present, and every later spec adds tables through a migration, never by
hand.

## User story

As the platform admin, I point the API at an empty database, run one command, and get a
schema that the release pipeline can upgrade in place on every deploy without losing data.

## Interface

Settings (env prefix `TABAYYUN_`):

| Key | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | dev default only | SQLAlchemy URL, `postgresql+psycopg://…`; prod refuses the dev default (exists) |
| `DB_POOL_SIZE` | `5` | pool size per process |
| `DB_POOL_MAX_OVERFLOW` | `10` | |
| `TIMESCALE` | `auto` | `auto` detects the extension, `on` requires it, `off` never creates hypertables (Apache-2-only mode, ADR-0003) |
| `TEST_DATABASE_URL` | unset | when set (`TABAYYUN_TEST_DATABASE_URL`), `pytest` runs the database tests against it; otherwise they skip |

Commands: `make db-upgrade` (`cd api && uv run alembic upgrade head`), `make db-revision
m="message"` (autogenerate), `make dev-infra` (existing compose with TimescaleDB).

Package layout:

```
api/src/tabayyun/db/__init__.py               engine factory, session dependency, `Base`, health check, schema guard
api/src/tabayyun/db/models.py                 SQLAlchemy 2 declarative models
api/src/tabayyun/db/migrate.py                programmatic Alembic (upgrade/downgrade/check, `python -m tabayyun.db.migrate`)
api/src/tabayyun/db/migrations/               env.py, versions/0001_initial.py (packaged with the wheel)
api/alembic.ini                               CLI convenience for developers, points at the packaged scripts
```

Edited during implementation: the migration scripts live inside the package rather than in
`api/alembic/` so that the api image, which installs the wheel and has no repository
checkout, can run them from `python -m tabayyun.db.migrate`; `alembic.ini` stays at the api
root for the CLI.

Tables (all ids `uuid` primary keys generated in Python; timestamps `timestamptz`; every
tenant table carries `org_id` and `workspace_id` so row-level security in spec 007 is one
policy per table):

| Table | Columns |
|---|---|
| `orgs` | id, name, created_at |
| `workspaces` | id, org_id → orgs, name, timezone (default `UTC`), created_at; unique (org_id, name) |
| `sources` | id, org_id, workspace_id, type (`upload`, `csv_dir`, `pi_web_api`, `opc_ua`), name, config jsonb (never secrets), credentials_ref text null, health jsonb, created_at; unique (workspace_id, name) |
| `series` | id, org_id, workspace_id, source_id → sources, external_id, name, unit, kind (`measurement`, `counter`, `setpoint`, `status`), expected_interval_ns bigint, physical_min, physical_max, operational_min, operational_max, resolution, non_negative bool null, asset_path, metadata jsonb, created_at, updated_at; unique (source_id, external_id) |
| `datasets` | id, org_id, workspace_id, name, selection jsonb, window_policy jsonb, created_at |
| `dataset_series` | dataset_id, series_id; primary key (dataset_id, series_id) |
| `runs` | id, org_id, workspace_id, dataset_id null, trigger (`upload`, `suite`, `manual`), status (`queued`, `running`, `succeeded`, `failed`), window_start, window_end, now_ns bigint, started_at, finished_at, stats jsonb, error text, created_at |
| `uploads` | run_id → runs (pk), filename, content_type, size_bytes, data bytea, params jsonb, created_at |
| `findings` | id, org_id, workspace_id, series_id → series, check_id, dimension, severity, window_start, window_end, score_impact, summary, evidence jsonb, status (`open`, `acked`, `muted`, `resolved`), status_reason, status_at, first_run_id, last_run_id, occurrences int default 1, created_at, updated_at; primary key (id, window_start) |
| `metrics` | series_id, run_id, check_id, name, ts, value double; primary key (series_id, check_id, name, ts) |
| `scores` | series_id, run_id, layer (default `raw`), method_version, overall, dimensions jsonb, n_findings int, computed_at; primary key (series_id, layer, computed_at) |
| `coverage` | series_id, layer, range_start, range_end, rows bigint, written_at; primary key (series_id, layer, range_start) |

Indexes: `findings (workspace_id, status, window_start desc)`, `findings (series_id,
check_id, window_start desc)`, `metrics (series_id, name, ts desc)`, `scores (series_id,
computed_at desc)`, `runs (workspace_id, created_at desc)`, `series (workspace_id, name)`.

Hypertables when TimescaleDB is present: `findings` on `window_start`, `metrics` on `ts`,
`scores` on `computed_at`, chunk interval 30 days.

Seed: migration `0001` inserts one org `default` and one workspace `default` with fixed UUIDs
(`00000000-0000-0000-0000-000000000001` / `…0002`) so that sprint 6 and 7 can persist without
the tenant model from spec 007; spec 007 migrates real memberships onto them.

## Behaviour

1. `create_app()` builds one async engine from `DATABASE_URL` at startup and disposes it at
   shutdown; request handlers get a session through a FastAPI dependency; every session is a
   transaction that commits on success and rolls back on any exception.
2. `alembic upgrade head` is idempotent: running it twice makes no change. `alembic check`
   (or `alembic revision --autogenerate` producing an empty diff) proves models and
   migrations agree; CI fails when they drift.
3. With `TIMESCALE=auto`, migration `0001` runs `CREATE EXTENSION IF NOT EXISTS timescaledb`
   and converts the three tables when the extension is available, else logs one warning and
   continues. With `on`, a missing extension is a migration error. With `off`, the extension
   is neither created nor used even if installed.
4. `/healthz` gains a `db` field: `ok` when `SELECT 1` succeeds within 2 s, else `degraded`;
   the endpoint still returns 200 so that the container keeps running while the database
   restarts, and `/api/version` reports `schema_revision`.
5. The API refuses to start when the schema revision in the database is behind the newest
   migration (logs the two revisions and exits 3), so an old container never writes into a
   newer schema and a new container never runs on an old one. The api image runs
   `alembic upgrade head` as its entrypoint before `uvicorn`; the CapRover and compose docs
   say so.
6. No table stores secrets: `sources.credentials_ref` names a key in the environment or a
   secret store, never the value.

## Acceptance criteria

- [x] `alembic upgrade head` on an empty Postgres 17 without TimescaleDB creates every table
      above and the seed rows; a second run is a no-op.
- [x] The same on TimescaleDB makes `findings`, `metrics` and `scores` hypertables
      (`timescaledb_information.hypertables` lists them); with `TIMESCALE=off` it does not.
- [x] `alembic check` passes in CI against a Postgres service container.
- [x] `/healthz` reports `db: degraded` when the database is unreachable and the process keeps
      serving; `/api/version` includes `schema_revision`.
- [x] Starting the API against a database at an older revision exits with code 3 and a log line
      naming both revisions.
- [x] `TABAYYUN_DATABASE_URL` with the dev default is refused in prod (existing rule).
- [x] Database tests skip cleanly when `TEST_DATABASE_URL` is unset and run when it is set.
- [ ] The deployed CapRover api app runs the migration on start and serves the new
      `/api/version` field. (Checked on the first deploy after merge; ticked in the spec 002 PR.)

## Test cases

Unit (`api/tests/test_settings.py`): `test_timescale_mode_values`, `test_pool_settings`.

Database (`api/tests/db/`, skipped without `TEST_DATABASE_URL`, run in CI with a
`timescale/timescaledb:2.30.1-pg17` service):
- `test_migrate_fresh_and_idempotent`: upgrade twice, compare `alembic current`.
- `test_models_match_migrations`: autogenerate diff is empty.
- `test_hypertables_created` / `test_hypertables_skipped_when_off`.
- `test_seed_rows_present`.
- `test_session_rolls_back_on_error`.
- `test_startup_refuses_old_schema`: downgrade one step, the startup guard (`guard_schema`, run
  by the app lifespan) raises `SystemExit(3)`; `create_app()` itself opens no connection so
  that tests without a database can still build the app.

CI: the `python api` job gains the service container and sets `TEST_DATABASE_URL`.

## Out of scope

- Row-level security policies and memberships (spec 007, sprint 8).
- Backups and restore runbook (sprint 13).
- Parquet cache files; `coverage` is created here but written by spec 006 (sprint 7).
