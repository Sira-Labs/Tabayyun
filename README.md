# Tabayyun

> *Tabayyun* (تبيّن): to verify a report before acting on it.

Tabayyun is a self-hostable platform that continuously verifies the trustworthiness of
industrial time series: it profiles every series, runs a curated library of statistical and
domain checks, scores the result per quality dimension, explains why a series is
untrustworthy, routes findings to the people who can fix them, and lets them correct windows
with full lineage and deliver the corrected series downstream. First target vertical:
**energy** (PV, wind, grid, metering, storage).

Status: **R0 — research and design complete; implementation starts in R1.**

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
