# ADR-0003: Postgres + TimescaleDB for metadata and results; Parquet for raw cache

- **Status:** Accepted
- **Date:** 2026-09-21

## Context
Two storage needs: transactional metadata plus append-heavy result rows (findings, metrics,
scores), and a large immutable cache of raw observations for re-runs and charts. Must be
easy to install and back up in an air-gapped site.

## Decision
One PostgreSQL 17 with TimescaleDB (Community edition) holds metadata, RBAC, audit, job
queue and result hypertables. Raw observations are cached as Parquet files (local disk or
S3-compatible) partitioned by source/tag bucket/month, read by Polars and DataFusion.
An "Apache-2-only" mode disables Timescale Community features for customers whose legal
review rejects the Timescale License (self-hosted use is permitted; offering as a service is not).

## Alternatives considered
| Option | Pros | Cons | Why not |
|---|---|---|---|
| ClickHouse / QuestDB for raw | Fast ad-hoc analytics | Second stateful server | Operational cost in on-prem installs; offered as a *connector* instead |
| InfluxDB 3 Core | Rust/Arrow native | 72-hour query window limit in Core; Enterprise not redistributable | Not viable |
| Postgres for raw too | One system | Heavy at 100k tags × 1 s × years | Cache size and ingest cost |

## Consequences
Backups = Postgres dump + cache directory copy. The cache is disposable and rebuildable from
sources; findings are not.
