# ADR-0009: User-defined checks in three tiers (YAML, SQL, sandboxed Python)

- **Status:** Accepted
- **Date:** 2026-09-21

## Context
Process engineers and data scientists must be able to add checks without waiting for a
release, without compromising the host.

## Decision
1. **YAML** parameterises built-in checks (thresholds, windows, selectors, `for each`).
2. **SQL** checks run read-only against the Parquet cache via DataFusion with statement
   timeouts and row limits; returned rows are findings.
3. **Python** checks are functions `(polars.DataFrame, params) -> CheckResult` executed in a
   separate worker pool with cgroup CPU/memory limits, no network, read-only filesystem and
   a seccomp profile; results come back as Arrow over a pipe. Plugins declare a manifest
   identical to built-in checks so evidence, scoring and docs are uniform.
A WASM lane (wasmtime/extism) is reserved for compiled plugins; Python-in-WASM is not
planned as a primary path.

## Alternatives considered
| Option | Pros | Cons | Why not |
|---|---|---|---|
| RestrictedPython in-process | Simple | Not a security boundary | Unsafe |
| Pyodide / MicroPython in WASM | Strong isolation | No numpy/polars; server-side Pyodide unsupported | Not practical |
| gVisor / Firecracker | Strongest | Heavy for on-prem installs | Reserve for multi-tenant SaaS |

## Consequences
Plugin execution is slower than built-ins and is scheduled on dedicated workers. Plugin
authors get a local test harness with synthetic data generators.
