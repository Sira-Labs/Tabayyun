# ADR-0002: Checks are Polars lazy plans, Arrow kernels only when necessary

- **Status:** Superseded by ADR-0015 (2026-09-23)
- **Date:** 2026-09-21

## Context
Need composable, vectorised, memory-bounded execution of ~100 checks across many series,
with time-series primitives (dynamic group-by, rolling, asof join, upsample).

## Decision
Each check implements `plan(LazyFrame, params) -> LazyFrame` producing the standard
`(series_id, window_start, window_end, statistic, flag, detail)` frame. Plans are composed
per batch and executed on the Polars streaming engine. Hot loops that Polars cannot express
(e.g. online changepoint scoring, Hampel with adaptive window) are Arrow compute kernels
called from the plan via expression plugins. DataFusion is used only for SQL-style checks and
the explorer.

## Alternatives considered
| Option | Pros | Cons | Why not |
|---|---|---|---|
| DataFusion UDFs for everything | SQL surface, streaming | No rolling/dynamic windows, verbose for statistics | Clumsy for per-series pipelines |
| Hand-written Arrow kernels for everything | Full control | Re-implementing group-by/rolling; more code, more bugs | Wasteful |

## Consequences
Pin Polars exactly; budget an upgrade PR roughly every two months. Row-order-sensitive
checks must opt in to ordered execution under Polars 2.0 streaming.
