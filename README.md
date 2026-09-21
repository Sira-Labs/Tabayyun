# Tabayyun

> *Tabayyun* (تبيّن): to verify a report before acting on it.

Tabayyun is a self-hostable platform that continuously verifies the trustworthiness of
industrial time series: it profiles every series, runs a curated library of statistical and
domain checks, scores the result per quality dimension, explains why a series is
untrustworthy, routes findings to the people who can fix them, and lets them correct windows
with full lineage and deliver the corrected series downstream. First target vertical:
**energy** (PV, wind, grid, metering, storage).

Status: **R1 sprint 1 in progress.** The Rust core runs the first eight structural checks
end to end on CSV/Parquet files; API and web are skeletons.

## Quick start

```bash
# Rust core: generate a faulty synthetic series and run all built-in checks on it
cd core && cargo run -q --bin tabayyun -- synth --out /tmp/f.csv --faults gap,flatline,nans,negative
cargo run -q --bin tabayyun -- run --input /tmp/f.csv --quality-col quality --unit "m3/h" --pretty

# API (FastAPI) and web (Vite) in development
make dev-infra            # Postgres+TimescaleDB, Keycloak (docker compose)
make api-dev              # http://localhost:8000/api/docs
make web-dev              # http://localhost:5173
```

Checks implemented so far: `tby.completeness`, `tby.staleness`, `tby.timestamp_integrity`,
`tby.sampling_regularity`, `tby.value_type`, `tby.flatline`, `tby.physical_range`,
`tby.non_negative` (catalogue #1, 2, 4, 5, 7, 8, 9, 11).

## Documentation

| Area | Document |
|---|---|
| Vision, personas, scope | [docs/architecture/01-product-vision.md](docs/architecture/01-product-vision.md) |
| Domain model and scoring | [docs/architecture/02-domain-model.md](docs/architecture/02-domain-model.md) |
| System architecture | [docs/architecture/03-system-architecture.md](docs/architecture/03-system-architecture.md) |
| Authorization and sharing | [docs/architecture/04-authz-and-sharing.md](docs/architecture/04-authz-and-sharing.md) |
| Check specification (manifest, contract, thresholds) | [docs/checks/00-check-specification.md](docs/checks/00-check-specification.md) |
| **The first 30 checks** | [docs/checks/catalogue.md](docs/checks/catalogue.md) |
| Frontend design | [docs/frontend/01-frontend-design.md](docs/frontend/01-frontend-design.md) |
| Security baseline (ASVS L2) | [docs/frontend/02-security-baseline.md](docs/frontend/02-security-baseline.md) |
| Roadmap and repo layout | [docs/roadmap/roadmap.md](docs/roadmap/roadmap.md) |
| Decisions (ADRs 0001–0010) | [docs/adr/](docs/adr/) |

Research reports (with verified sources):

1. [State of the art in time-series data quality](docs/research/01-sota-timeseries-quality.md)
2. [Energy-domain data quality: metering, PV, wind, grid, storage, markets](docs/research/02-energy-domain-quality.md)
3. [Frontend, auth, RBAC, sharing and security stack](docs/research/03-frontend-auth-security.md)
4. [Backend, Rust compute core, storage, connectors, plugins](docs/research/04-backend-core-stack.md)

## Stack (decided)

- **Core:** Rust (Polars lazy/streaming, Arrow, augurs, statrs), shipped as a PyO3 wheel; Parquet cache.
- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2 async, Procrastinate; PostgreSQL 17 + TimescaleDB.
- **Identity:** external OIDC provider (Keycloak default, Zitadel supported); Google login and enterprise SSO brokered; backend-for-frontend cookie sessions.
- **Frontend:** Vite + React 19 SPA, TanStack Router/Query, shadcn/ui + Tailwind v4, uPlot; PWA; Capacitor wrapper later.
- **Authorization:** org → workspace → resource RBAC in Postgres with row-level security; share by user, team, or hashed link token.
- **Deployment:** docker compose bundle (air-gap tarball) and Helm chart.

## Principles

Explain, don't just flag · opinionated defaults with transparent thresholds · series first ·
local by default · secure by design · extensible with sandboxed Python checks.

## Licence

To be decided before R1 code lands.
