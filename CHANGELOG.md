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
- Release pipeline publishing `tabayyun-api` and `tabayyun-web` images to GHCR with SBOM and
  provenance; production compose bundle with Caddy.
- Apache-2.0 licence (ADR-0012), contribution guide, security policy, code of conduct, issue
  and pull request templates, Dependabot, CODEOWNERS and the `main` branch ruleset.

[Unreleased]: https://github.com/thedatadudech/Tabayyun/commits/main
