# ADR-0015: Checks are Rust kernels over Arrow buffers; no Polars in the core

- **Status:** Accepted (supersedes ADR-0002)
- **Date:** 2026-09-23

## Context
ADR-0002 planned every check as a Polars lazy plan, with hand-written Arrow kernels only
where Polars could not express a hot loop. Sprints 1–4 built the first 20 checks
differently: each is a sequential kernel over the `SeriesFrame` buffers (`ts: Vec<i64>`,
`values: Vec<f64>`, `quality: Vec<Quality>`), and the crate depends on `arrow` only at its
boundary. The deviation was never recorded. Sprint 7 adds the Parquet cache and
cross-series checks, which is the point where Polars would have entered, so the choice is
made explicitly now.

Observations from sprints 1–6:
- Per-series checks need rolling windows, segments, runs and robust statistics over one
  sorted series. The helpers in `checks/mod.rs` (`runs_where`, `segments`, `episodes`,
  `unusual_among`) cover them in about 150 lines; no check needed a general group-by.
- Cross-series checks need series aligned on a common time grid. With sorted inputs this is a
  merge, not a join engine.
- The Rust `polars` crate carries its own Arrow implementation (`polars-arrow`), distinct
  from `arrow` 59, which is pinned together with `pyo3-arrow`. Adding it means a third
  version set to move in lockstep, conversions at the boundary, a several-fold compile time
  and a much larger wheel.
- Python callers can already hand Polars and pyarrow frames to the core through the Arrow
  PyCapsule interface (tested in `core/tabayyun-py`).

## Decision
Checks, profiling, scoring and alignment are Rust kernels over `SeriesFrame` buffers. The core
uses `arrow` 59 and `parquet` 59 (with `object_store` for the cache) and no Polars. Polars
stays a supported *input* at the Python boundary.

Out-of-core execution (100k series × one year that does not fit in memory) is decided at the
S13-3 performance run, with measurements, between streaming batches through the existing
kernels, DataFusion (already planned for the SQL explorer) and Polars.

## Alternatives considered
| Option | Pros | Cons | Why not |
|---|---|---|---|
| Keep ADR-0002, migrate the 20 checks to lazy plans | Composable plans, streaming engine | About a sprint of rewriting with no behaviour change; second Arrow stack; compile time and wheel size | Cost without an observed need |
| Polars for new code only (cache reads, alignment) | Uses Polars where it is strongest | Pays the whole dependency cost for two small features | Same cost, little gain |
| DataFusion for checks | Streaming, SQL surface | Same objections as in ADR-0002: rolling and per-series logic is clumsy | Kept for SQL checks and the explorer only |

## Consequences
- One Arrow version set (`arrow`, `parquet`, `pyo3-arrow`, and `object_store` at the version
  `parquet` resolves) is bumped together, as `CLAUDE.md` already requires for Arrow.
- Kernels keep the ADR-0002 rule that checks are pure: the new `cache` module does I/O and
  lives outside `checks`.
- The architecture document's "Polars lazy/streaming engine" line is replaced by this ADR's
  wording.
- Revisit at S13-3 if memory or throughput targets are missed.
