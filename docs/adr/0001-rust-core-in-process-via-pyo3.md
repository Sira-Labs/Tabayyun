# ADR-0001: Rust compute core embedded in Python via PyO3

- **Status:** Accepted
- **Date:** 2026-09-21

## Context
Checks must run over 100k+ series and years of history; the API, connectors and user
plugins are Python. Options: Rust as an in-process library (PyO3 wheel) or as a separate
service (Arrow Flight / gRPC).

## Decision
Ship `tabayyun_core` as a PyO3 + maturin wheel (abi3, Python 3.10–3.13). Data crosses the
boundary as Arrow C stream capsules (pyo3-arrow), zero-copy. The Rust API is designed around
`RecordBatch`/`LazyFrame` in and out so the same crate can later be wrapped in an axum/tonic
Arrow Flight server without changing check code. The GIL is released for all compute.

## Alternatives considered
| Option | Pros | Cons | Why not |
|---|---|---|---|
| Separate Rust service + Arrow Flight | Process isolation, horizontal scaling | Second daemon, mTLS, IPC per window, harder air-gap | Premature; revisit when one node is insufficient |
| Pure Python (Polars/NumPy) | Fastest to write | No control over memory, slower kernels, harder to ship as one artefact | Loses the performance moat |
| Go core | Easy static binary | Weak numerics/Arrow ecosystem | Not competitive for statistics |

## Consequences
Rust panics or OOM take the worker process down: workers are separate from the API process
and supervised. Polars Rust API churn requires a pinned version and an adapter layer.
