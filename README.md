# Tabayyun

> *Tabayyun* (تبيّن): to verify a report before acting on it.

Tabayyun is a self-hostable platform that continuously verifies the trustworthiness of
industrial time series: it profiles every series, runs a curated library of statistical and
domain checks, scores the result per quality dimension, explains why a series is
untrustworthy, routes findings to the people who can fix them, and lets them correct windows
with full lineage and deliver the corrected series downstream. First target vertical:
**energy** (PV, wind, grid, metering, storage).

Status: **R1 sprint 3 in progress.** The Rust core runs 20 of the 30 catalogue checks, ships
as a Python wheel, the API + web run the checks on an uploaded CSV, and the release
pipeline publishes deployable images to GHCR (see `deploy/README.md`).

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

Checks implemented so far (catalogue numbers in brackets): `tby.completeness` (1),
`tby.staleness` (2), `tby.timestamp_integrity` (4), `tby.sampling_regularity` (5),
`tby.quality_flags` (6), `tby.value_type` (7), `tby.flatline` (8), `tby.physical_range` (9),
`tby.operational_range` (10), `tby.non_negative` (11), `tby.spikes` (13),
`tby.rate_of_change` (14), `tby.resolution_loss` (16), `tby.interpolation_artifacts` (17),
`tby.latency` (3), `tby.scale_shift` (12), `tby.noise_level` (15), `tby.level_drift` (18),
`tby.distribution_drift` (19), `tby.changepoint` (20).

From Python:

```python
import tabayyun_core as tc          # pip install from core/tabayyun-py (maturin)
batch = tc.synth(n=2880, faults=["gap", "flatline", "spikes"])   # or any pyarrow/polars frame
report = tc.run_checks(batch, {"id": "demo", "unit": "m3/h"}, quality_col="quality")
print(report["score"]["overall"], len(report["findings"]))
```

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
| Deployment (images, compose, CD) | [deploy/README.md](deploy/README.md), [CapRover](deploy/caprover.md) |
| Decisions (ADRs 0001–0012) | [docs/adr/](docs/adr/) |

Research reports (with verified sources):

1. [State of the art in time-series data quality](docs/research/01-sota-timeseries-quality.md)
2. [Energy-domain data quality: metering, PV, wind, grid, storage, markets](docs/research/02-energy-domain-quality.md)
3. [Frontend, auth, RBAC, sharing and security stack](docs/research/03-frontend-auth-security.md)
4. [Backend, Rust compute core, storage, connectors, plugins](docs/research/04-backend-core-stack.md)
5. [Oil and gas data quality: wells, historian compression, fiscal and allocation metering, pipelines](docs/research/05-oil-gas-domain-quality.md)

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

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow, [SECURITY.md](SECURITY.md) for
reporting vulnerabilities privately, and the [milestones](https://github.com/thedatadudech/Tabayyun/milestones)
for what is planned next.

## Licence

[Apache License 2.0](LICENSE) (ADR-0012). Contributions are accepted under the same licence.
