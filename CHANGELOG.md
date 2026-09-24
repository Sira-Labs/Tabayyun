# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Until 1.0, minor versions may break APIs.

## [Unreleased]

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
- Release pipeline publishing `tabayyun-api` and `tabayyun-web` images to GHCR with SBOM and
  provenance; production compose bundle with Caddy.
- Apache-2.0 licence (ADR-0012), contribution guide, security policy, code of conduct, issue
  and pull request templates, Dependabot, CODEOWNERS and the `main` branch ruleset.

### Changed
- The repository moved to `Sira-Labs/Tabayyun`; images are published as
  `ghcr.io/sira-labs/tabayyun-api` and `ghcr.io/sira-labs/tabayyun-web` (was `ghcr.io/thedatadudech/…`).
- `tby.physical_range` reports one finding per episode of nearby excursions with the exact
  count, and one summary above 20 episodes, instead of one finding per excursion (spec 016).
  Open findings of the old shape do not merge with the new ones.
- The CLI reads epoch-integer timestamps in one unit per column (`--ts-unit`, inferred from the
  median otherwise) and exits 2 on instants outside 1971–2199, as the API does; integer Parquet
  timestamp columns are no longer taken as nanoseconds (spec 017, ADR-0014).
- The CLI reads brotli, gzip and lz4 Parquet as well as snappy and zstd.
- `TABAYYUN_CACHE_DIR` is now `TABAYYUN_CACHE_URL` (the old name is still accepted).
- Checks are Rust kernels without Polars in the core (ADR-0015 supersedes ADR-0002).

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

[Unreleased]: https://github.com/Sira-Labs/Tabayyun/commits/main
