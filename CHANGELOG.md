# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Until 1.0, minor versions may break APIs.

## [Unreleased]

### Added
- Check suites (spec 027):
  - A suite names a dataset, a cron schedule (UTC, at most every five minutes) and the checks
    to run with their parameters. The worker starts a run for every due suite each minute; a
    suite whose previous run has not finished waits for its next time, and downtime is
    followed by one run.
  - `/suites` lists the suites with schedule in words, next and last run; a suite's page
    shows every threshold with where it comes from (override, default, the series' metadata
    or its baseline), "Run now", enable or disable, delete, and the run history.
  - `/api/suites` (create, read, list, change, delete, `POST …/runs`), `GET
    /api/checks/catalogue` with every check's defaults, and `GET /api/runs?suite_id=`. Runs
    carry `suite_id` and `trigger = "suite"`. Changes are written to the audit log.
  - The core gained the check catalogue and `validate` for check configurations; migration
    0012 adds `check_suites` and the runs' suite columns, 0013 the index for a suite's history.
- Baseline profiles (spec 028):
  - Every series with cached data gets a baseline: the profile of its trailing 28 days,
    without bad-quality samples and without the windows of its open critical findings.
    Baselines are stored with a version; the newest 30 are kept.
  - The worker refreshes each series about once a day (an hourly task queues those that are
    due, one job at a time); editors can refresh one at once.
  - The series page shows the baseline: version, window, what was used and left out, and the
    thresholds it gives the checks with the change against the version before.
  - `GET /api/series/{id}/baselines` and `POST /api/series/{id}/baselines`; dataset runs name
    the versions they used in `stats.baselines`. Migration 0014 adds `series_baselines`.
- Findings inbox (spec 029):
  - `/findings` lists the findings of a workspace across all series, filtered by status,
    severity, dimension, check, assignee, feedback and text, with the filter in the URL.
    Findings can be grouped by check, series or asset; the table loads while scrolling and
    draws only the visible rows.
  - Editors acknowledge, mute until a date with a reason, resolve, reopen, assign and mark
    false positives, one finding or many at once; what could not be changed is named. An
    expired mute opens the finding again.
  - Filters can be saved per user, and the current list exported as CSV.
  - Every change of a finding is written to the audit log with the acting user.
  - `GET /api/findings` gained filters and returns series name, asset path, assignee, the end
    of a mute and feedback; new are `/api/findings/groups`, `/bulk`, `/assignees`, `/export`
    and `/api/finding-filters`. Migration 0015 adds the columns and `finding_filters`.
- Live events (spec 030):
  - `GET /api/events` is a server-sent event stream of the workspace: `run.completed` when a
    run succeeded or failed (also when the reaper fails a lost run), `finding.created`
    when a run stored new findings, and `run.updated` when an upload's cache and coverage
    were written after it completed. Events travel through Postgres `LISTEN`/`NOTIFY`, so the
    worker's runs reach the browsers of every api process, and only with the commit that
    stored them.
  - The web app holds one stream per page frame and reloads runs, findings, series and
    suites when their events arrive: the findings inbox and the runs list update without a
    reload. Without the stream the pages work as before.
  - `TABAYYUN_EVENTS_MAX_STREAMS` (200), `TABAYYUN_EVENTS_HEARTBEAT_S` (15) and
    `TABAYYUN_EVENTS_MAX_AGE_S` (900) bound the streams of an api process. Migration 0016
    replaces the reaper function; no table changes.

### Changed
- `PATCH /api/findings/{id}` refuses fields it does not know (it ignored them before).
- Dataset runs judge a series against its stored baseline once it has one, no longer against
  the window they are checking. Rate-of-change, spike, noise, drift and resolution findings of
  a series can change with its first baseline. Upload runs are unchanged.
- `tby.spikes` takes the resolution for its scale floor from the baseline when the series
  declares none, no longer from the window under test.
- The api image runs on Python 3.15 (was 3.14) and its build stage on Rust 1.100. CI now
  builds the images on pull requests that touch a Dockerfile and starts the api image once.

## [0.3.0] - 2026-11-15

Sprint 9: connectors and the series screen. The patch releases v0.2.1 to v0.2.10 carried
these changes to staging as they merged.

### Added
- Compare view (spec 018):
  - `/compare` shows 2 to 8 series on one time axis, with the whole view in the URL: one plot
    with two axes for up to two units, stacked lanes that share cursor and zoom for more.
  - Values can be shown raw, scaled to their range, as z-scores or as an index, computed from
    the window's raw samples; the readout keeps raw values and units.
  - Balances and pairs get a residual lane (inputs minus outputs, or A − B) with the loss
    band. Cross-series findings are shaded across all lanes, and a selected finding draws its
    suspect thicker and labelled.
  - A table view with CSV export, a keyboard cursor with a spoken readout, and links from
    findings and the series page ("Compare with…").
  - `GET /api/compare/chart` returns M4 points of the series on identical bins with each
    series' window statistics; `GET /api/series-groups/{id}/residual` returns the residual
    and its band. The core gained `residual`, `residual_chart` and `window_stats`.
- Connector framework (spec 021):
  - Connector sources are created and changed, enabled or disabled, given credentials, given
    series, checked and fetched under `/api/sources`, with a fetch history.
  - `fetch_window` fills coverage gaps: calls cut to the connector's limits, paced per
    source, retried on transient errors, with health per source.
  - Sources poll on their own `poll_interval_s`.
  - Dataset runs fetch what the cache lacks before checking.
  - The built-in `synthetic` connector generates deterministic series with optional faults.
- Connector credentials are write-only and encrypted with AES-256-GCM under
  `TABAYYUN_MASTER_KEY`, with key rotation (`python -m tabayyun.secrets rotate`).
- PI Web API connector (spec 022):
  - A `pi_web_api` source reads an AVEVA PI System over https, with Basic or bearer
    credentials and an optional private certificate authority.
  - Point search by name covers the PI Data Archive and, when configured, the PI Point
    attributes of an AF database.
  - Recorded values are paged past PI's silent `maxCount` cut. PI's quality flags and system
    states map to good, uncertain, bad and estimated.
  - The metadata import fills unit, limits and asset path. It keeps PI's exception and
    compression settings in the series metadata.
- Connector sources gain `POST /api/sources/{id}/search`, `POST /api/sources/{id}/metadata`
  and `GET /api/sources/{id}/fetches/{fetch_id}` (spec 022).
- OPC UA connector (spec 023, `asyncua` under ADR-0018):
  - An `opc_ua` source reads raw history from an OPC UA server, signed or signed and
    encrypted, with a client certificate generated per source
    (`GET /api/sources/{id}/client-certificate`).
  - The server certificate is pinned by its SHA-256 thumbprint.
  - Search browses the address space; history reads follow continuation points.
  - Status codes map to the four qualities: `Good_LocalOverride` and `Good_Clamped` count as
    uncertain, `Uncertain_SubstituteValue` as estimated.
  - The metadata import reads `EngineeringUnits`, `InstrumentRange`, `EURange` and the
    browse path.

- Web pages for connector sources and the series catalogue (spec 024):
  - `/sources` lists sources with health and last fetch, and `/sources/new` creates them.
  - `/sources/:id` checks, fetches, finds and adds points, imports metadata, holds the
    write-only credentials and settings, and pins OPC UA certificates.
  - `/series` searches and filters by source, unit, kind and score, with the filters in the URL.
- `GET /api/series` filters by `unit` and `score_max`. `GET /api/series/units` lists units, and
  `GET /api/sources` includes each source's `last_fetch`.
- Series page with chart (spec 025):
  - `/series/:id` draws the series with uPlot: findings as shaded windows, a quality rug,
    the operating band and the physical limits.
  - Presets, date inputs, drag-to-zoom, Back and the arrow, `+`, `−` and Home keys set the
    range, which lives in the URL.
  - Beside the chart are the series' metadata and its profile.
  - Catalogue rows and run reports link to the page.
- `GET /api/series/{id}/chart` returns M4-downsampled points on bins of the requested window,
  with gap breaks and quality runs, an ETag, and a row cap (`TABAYYUN_CHART_MAX_ROWS`).
  `GET /api/series/{id}/profile` profiles a window without bad samples and gives the operating
  band (spec 025).
- File-drop sources (spec 026):
  - A `csv_dir` source reads CSV and Parquet files dropped into a folder under
    `TABAYYUN_FILE_SOURCES_ROOT`, in a long or a wide layout, for sites without a network path.
  - Scans run on the source's poll interval or from **Scan now**
    (`POST /api/sources/{id}/ingest`). They create the series files name, write them to the
    cache with coverage, skip files already read, and read changed files again.
  - `GET /api/sources/{id}/files` and the source page list every file with its rows, series and
    error.
  - `deploy/compose.yaml` mounts `TABAYYUN_DROP_DIR` read-only into the worker.

### Changed
- A source check stores what it found as the job's `result`; a file drop's check counts its files
  (spec 026).
- A point a connector cannot read no longer fails a fetch. The other points are fetched, and
  the fetch ends `partial` with the failed points in `result.point_errors`. The `synthetic`
  connector reports unconfigured points this way instead of returning nothing (spec 022).
- Dependencies: SQLAlchemy 2.1 (the API no longer uses the deprecated `Result.tuples()` and
  `select().distinct(column)`), Procrastinate 3.11 with migration 0011, which installs its
  faster `procrastinate_fetch_job_v2`, FastAPI 0.163, pydantic 2.14, pyarrow 26 and
  `@vitejs/plugin-react` 6.

### Security
- Connectors only reach addresses the network policy allows. Loopback, link-local and cloud
  metadata addresses are always refused, and private ranges unless listed in
  `TABAYYUN_CONNECTOR_ALLOWED_NETWORKS`. HTTP clients connect to the checked address and do
  not follow redirects.
- OPC UA connections refuse an unpinned or changed server certificate. They are unencrypted
  only with an explicit `allow_insecure`, and the client key never leaves the encrypted
  credentials (spec 023).
- Dependencies with published advisories are updated, so the blocking audits in CI pass
  again (they had failed on every pull request since 19 Oct):
  - Rust: `rustls` 0.23.46, `rustls-webpki`, `quinn-proto`, `zstd` 0.13.5 with `zstd-safe`
    7.4.1, `tokio` 1.54.1, `rustix`, and Arrow and Parquet 59.3.2 (still major 59).
  - Python: `certifi` 2026.10.23 and `joserfc` 1.8.0.
  - Web: `vite` 8.3.8 and, through `jsdom` and the lint plugins, `undici` 8.12.4 and `zod`
    4.6.7, with the other minor and patch updates of the web packages.

### Fixed
- A cache store setting with a space or quotes (for example `"http://…:9000"`) made the S3
  client panic, and series charts answered 500. Store settings are now trimmed and quotes
  refused at start; the core refuses an invalid endpoint or bucket name when it opens the store;
  a panic inside the store client becomes a cache error (503 `cache_unavailable`), and the API
  logs the store's message as `series.cache_read_failed`.
- The PI Web API connector's docstring no longer triggers a `SyntaxWarning` on start.

## [0.2.0] - 2026-10-05

Sprint 8: sign-in, tenants and the first security baseline pass.

### Added
- Rust core with checks 1–20 of the catalogue, baseline profile, scoring v2, M4
  downsampling, synthetic fault generator and the `tabayyun` CLI (CSV/Parquet in, JSON out).
- Python wheel `tabayyun_core` with Arrow PyCapsule in/out (pyarrow and polars).
- FastAPI service with `POST /api/checks/run` on an uploaded CSV; web page listing findings.
- Finding aggregation into episodes for recurring behaviour (ADR-0011).
- PostgreSQL schema with Alembic migrations and optional TimescaleDB hypertables; runs API
  (`POST/GET /api/runs`) executed by a Procrastinate worker (`TABAYYUN_ROLE=worker`).
- Stored findings, metrics and scores per run, with findings deduplicated across runs and a
  status lifecycle (`GET/PATCH /api/findings`, `GET /api/series/{id}/metrics|scores`,
  ADR-0013).
- Series and sources API (`GET /api/sources`, `GET/PATCH /api/series`) with stored metadata
  applied to later uploads; `ts_unit` for epoch-integer timestamps with a plausibility check
  (ADR-0014).
- Web app on persisted runs: runs list, upload form with physical limits and timestamp unit,
  and a run report that polls until done, with score tiles, findings with evidence, skipped
  checks and series metadata.
- Parquet cache of raw observations on local disk or S3-compatible storage (RustFS): upload
  runs write their series and record coverage; `tabayyun_core.Cache` and
  `tabayyun cache write|read|bench` (spec 006).
- Multi-series checks: series groups (`/api/series-groups`, kinds related, redundant and
  balance), datasets (`/api/datasets`) and dataset runs over the Parquet cache
  (`POST /api/runs {dataset_id}`); `tabayyun_core.run_checks_multi` and
  `tabayyun check-multi` (spec 008).
- `tby.correlation_break`: related or redundant series that stop agreeing, or whose lag
  moves, are reported per pair (spec 009, catalogue 22).
- `tby.redundant_disagreement`: redundant sensors that disagree beyond a tolerance, naming the
  one that is off when three or more vote (spec 010, catalogue 23).
- `tby.balance_residual`: a balance group whose residual leaves the expected loss band by more
  than the meters' uncertainty, naming the meter whose share changed (spec 011, catalogue 24).
- Seasonality in the baseline profile (`dominant_period_ns`, `seasonal_strength`) and
  `tby.seasonality_break`: a daily, weekly or yearly rhythm that weakens or changes period
  (spec 012, catalogue 21).
- Organisation admin (spec 014): invitations by email that join at sign-in (with SMTP email
  through the worker), member roles with a last-owner guard, teams, workspaces with access for
  people and teams, and an append-only audit log, under `/api/admin` behind a passkey sign-in
  from the last 12 hours; `GET /api/workspaces` and `X-Tabayyun-Workspace` choose the
  workspace of every data request; the web app's workspace picker and Admin pages.
- Release pipeline publishing `tabayyun-api` and `tabayyun-web` images to GHCR with SBOM and
  provenance; production compose bundle with Caddy.
- Apache-2.0 licence (ADR-0012), contribution guide, security policy, code of conduct, issue
  and pull request templates, Dependabot, CODEOWNERS and the `main` branch ruleset.

### Changed
- The repository moved to the Sira-Labs organisation (`github.com/Sira-Labs/Tabayyun`); images
  are now published as `ghcr.io/sira-labs/tabayyun-api` and `ghcr.io/sira-labs/tabayyun-web`.
  The release workflow spells the owner in lowercase, because GHCR rejects `Sira-Labs`.
- `tby.physical_range` reports one finding per episode of nearby excursions with the exact
  count, and one summary above 20 episodes, instead of one finding per excursion (spec 016).
  Open findings of the old shape do not merge with the new ones.
- The CLI reads epoch-integer timestamps in one unit per column (`--ts-unit`, inferred from the
  median otherwise) and exits 2 on instants outside 1971–2199, as the API does; integer Parquet
  timestamp columns are no longer taken as nanoseconds (spec 017, ADR-0014).
- The CLI reads brotli, gzip and lz4 Parquet as well as snappy and zstd.
- `TABAYYUN_CACHE_DIR` is now `TABAYYUN_CACHE_URL` (the old name is still accepted).
- Checks are Rust kernels without Polars in the core (ADR-0015 supersedes ADR-0002).

### Security
- Rate limits on sign-in and back-channel logout (per client IP), device and sign-out routes
  (per user, else per IP), admin changes (per user) and invitations (per organisation), counted in Postgres so api replicas share
  them; over the limit a request gets 429 with `Retry-After`. `TABAYYUN_RATE_LIMITS=false`
  turns them off for load tests (spec 015).
- The web edge sends HSTS, `Permissions-Policy`, CORP and `X-Frame-Options`, and its CSP no
  longer allows inline styles. API responses carry `nosniff`, `Cache-Control: no-store` and
  `Referrer-Policy: no-referrer` themselves, and the sign-in failure pages a strict CSP.
- The edge sets `X-Real-IP` from the client's address instead of passing on a client's own;
  only the proxies in `TABAYYUN_TRUSTED_PROXIES` may name it (none in the compose bundle).
- In `prod` the api and the worker exit with code 4 on a database login that bypasses
  row-level security (spec 007 warned).
- CI fails on a known advisory from `cargo audit`, `pip-audit` or `pnpm audit` (high and
  above), and runs them weekly; Rust builds use `--locked`; ESLint rejects
  `dangerouslySetInnerHTML`.

### Fixed
- Timestamps near the `i64` limits no longer overflow in the checks, the profile, the scorer,
  M4 or the cross checks; writing a sample at `i64::MAX` to the cache no longer hangs, and a
  group needing more than 20 million grid cells (bins times members) aligns to nothing instead
  of allocating them.
- A cache write whose last sample is at `i64::MAX` reads back in full: a range end of
  `i64::MAX` now has no end (issue #42). Dataset runs whose window or `now` lies after 2262
  run to that end instead of failing, and an upload's `now_ns` outside the `i64` range is a
  422 instead of a 500.
- A dataset window lying wholly outside 1677–2262, fixed or resolved at a run's `now`, is a
  422 instead of a run that fails in the worker.

[Unreleased]: https://github.com/Sira-Labs/Tabayyun/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/Sira-Labs/Tabayyun/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Sira-Labs/Tabayyun/releases/tag/v0.2.0
