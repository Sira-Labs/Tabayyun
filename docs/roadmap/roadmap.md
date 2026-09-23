# Roadmap

Effort assumes AI-assisted development. "Solo" = one senior engineer full time.

## R0 — Research and design (done 21 Sep 2026)

- Research reports 01–04, product vision, domain model, check specification, first-30
  catalogue, system architecture, authz/sharing design, frontend design, security baseline,
  ADRs 0001–0009.

Sprint breakdown with stories, actual dates and the forecast: `docs/roadmap/sprints.md`.

## R1 — Running core (sprints 1–13; forecast in `sprints.md`)

Progress log:
- **2026-09-21** sprint 1: Rust workspace, frame/profile/score/synth, checks 1, 2, 4, 5, 7,
  8, 9, 11 with tests, `tabayyun` CLI (CSV/Parquet in, JSON out), FastAPI and Vite skeletons, CI.
- **2026-09-21** sprint 2: extended baseline profile (p0.1/p99.9, rate p99.9, noise floor,
  linear fraction), checks 6, 10, 13, 14, 16, 17, M4 downsampling, PyO3 wheel `tabayyun_core`
  (Arrow PyCapsule in/out, pyarrow and polars tested), `POST /api/checks/run` on an uploaded
  CSV, web page that runs checks and lists findings with evidence, CI job for the bindings.
- **2026-09-21** sprint 3: checks 3 (latency, via an ingest-time column), 12, 15, 18, 19, 20
  (PELT on daily medians); scoring v2 merges overlapping findings per dimension; release
  pipeline builds `tabayyun-api` and `tabayyun-web` images to GHCR with SBOM and provenance,
  production compose bundle with Caddy (auto-TLS, security headers), optional SSH deploy job.
- **2026-09-21** sprint 4: release images fixed and deployed to CapRover
  (Hetzner) from GHCR; research 05 (oil and gas); CLI accepts historian-style timestamps and
  negative limits; `flatline` reports a frame frozen for its whole window even when the
  profile is constant; `linear_runs` no longer overlap (linear share was reported above
  100 % on compressed 1 Hz data); test datasets (UCI household, OPSD, Petrobras 3W) with
  measured expectations.
- **2026-09-22** sprint 5: finding aggregation into episodes (ADR-0011, PR #5), Apache-2.0
  licence and governance docs (ADR-0012, PR #7), Node 22 pin after a Dependabot Node 25
  bump broke the web image, weekly sprint plan (`sprints.md`), spec-driven workflow with
  specs 001–005 and `TASKS.md`, physical limits on the upload page.
- **2026-09-22 – 23** sprint 6 (planned for 27 Sep – 3 Oct): Postgres schema with Alembic and
  Timescale hypertables (spec 001), runs API with a Procrastinate worker as a fourth CapRover
  app (002), findings persisted with overlap deduplication and a lifecycle (003, ADR-0013),
  series and sources from uploads with metadata precedence and epoch timestamp units (004,
  ADR-0014, found by the live end-to-end check), web runs list, upload form and run report
  on persisted runs with Vitest in CI (005). Sprints 1–6 took three days, so the sprint plan
  was re-baselined from weekly sprints to scope sprints with a rolling forecast.

Goal: a self-hosted install that ingests from Parquet/CSV, PI Web API and OPC UA, runs the
30 checks on a schedule, scores series, shows findings in a dashboard, lets a user correct a
selected window with lineage and see the corrected layer re-scored, and lets a user log in
with Google.

The table below is the original R0 breakdown by layer. Sequencing, stories and dates now
live in `sprints.md`; the "Week" column is kept only as the design-time estimate.

| Week | Rust core | Python API / workers | Frontend | Platform |
|---|---|---|---|---|
| 1–2 | workspace, `SeriesFrame`, Parquet cache, profile, checks 1,2,4,5,7,8,9,11 with tests + fixtures + criterion | FastAPI skeleton, SQLAlchemy models, Alembic, Procrastinate, OIDC BFF against Keycloak, `authorize()` + RLS | Vite app shell, login, routing, generated client | compose bundle (api, worker, caddy, postgres+timescale, keycloak), CI (fmt, clippy, ruff, mypy, pytest, vitest, cargo audit, pip-audit) |
| 3–4 | baseline profile job; checks 6,10,13,14,16,17; PyO3 wheel with Arrow capsules; M4 downsampling | connector framework; Parquet/CSV and PI Web API connectors; runs, findings, scores persistence; SSE | overview, series catalogue, series detail with uPlot + findings overlay | synthetic data generator (Rust+Python parity); golden fixtures |
| 5–6 | checks 3,12,15,18,19,20 | OPC UA connector (asyncua first, Rust later); alert rules + email/webhook; audit log | findings inbox with triage actions; suites and threshold editing with "why this threshold" | Helm chart draft; air-gap tarball script; SBOM + image signing |
| 7–8 | checks 21–24 (related series, balance groups) | related-series suggestion job; workspace/member/team admin APIs; share by user/team | admin panel (workspaces, members, teams, audit); share dialog | pen-test prep; ASVS L2 self-assessment; threat model |
| 9–10 | energy pack 25–30 (solar position, bins); `repair` ops (mask, clamp, dedupe, impute.linear/like_day, align.resample, transform.affine) with lineage frames | corrections API (propose/approve/reject), corrected-layer versions in cache, re-scoring; share links (hashed tokens, locked scope) | "select window → correct" on the series chart with raw/corrected diff; link viewer; mobile layouts + PWA | performance run: 100k tags × 1 year synthetic; profiling |
| 11–12 | hardening, benchmarks, docs | plugin host (sandboxed Python checks, YAML DSL); Parquet/SQL publish targets for corrected series; reports export (CSV/PDF) | reports; polish, a11y pass, i18n scaffolding | beta install at one pilot site; release 0.1 |

Exit criteria: ≥95 % of injected faults in the synthetic corpus detected with ≤2 % false
positives per check; 100k series profiled in under one hour on 8 cores; all security
baseline items checked; one pilot installation running.

## R2 — Energy and oil-and-gas depth (sprints 14–18; forecast in `sprints.md`)

- Metering pack: VEE rule set (UBP/AEMO/Elexon presets), estimation methods with substitution
  types, DST interval-count rules, estimated-share reporting.
- PV pack: clear-sky and daily insolation limits, data shifts (capacity change), soiling and
  pyranometer drift vs reference, PR sensitivity to gaps.
- Wind pack: full IEC 61400-12-1 filtering presets, icing signature, implausible min/max/std,
  event-log correlation.
- Grid pack: PMU STAT-word decoding, completeness attributes per NASPI, state-estimation
  residual import, CGMES SHACL validation hook, market-interval count checks.
- Balance groups UI; **RepairFlows** (scheduled, block-based: filter → impute → align →
  publish) with approval policies; regulatory estimation methods with substitution codes
  (UBP/AEMO/Elexon precedence), `impute.seasonal`, `impute.kalman`, `reconcile.balance`;
  PI/OPC write-back to separate tags; human feedback → threshold suggestions.
- Oil and gas pack (`docs/research/05-oil-gas-domain-quality.md`): shut-in / valve-state
  masking series, PI ExcDev/CompDev/CompMax import with compression-aware `flatline`,
  `interpolation_artifacts` and `resolution_loss`, `redundant_disagreement` with SIS
  discrepancy override, `balance_residual` with VDI 2048-style propagated uncertainty,
  allocation imbalance and well-test-vs-MPFM checks, meter-factor drift from proving history,
  OPC `Good_LocalOverride` / `Good_Clamped` sub-findings, alarm-rate KPIs (EEMUA 191 /
  ISA-18.2).
- Public labelled benchmark corpus of DQ faults (synthetic + open datasets: PVDAQ,
  Kelmarsh/Penmanshiel, OPSD, Elia, Petrobras 3W) published under CC-BY.

## R3 — Platform (sprints 19–22; forecast in `sprints.md`)

- Enterprise SSO per org (OIDC/SAML), SCIM (after IdP preview testing), API tokens.
- Fleet baselines (template-level thresholds), operating-mode segmentation.
- Embeds with signed JWT; Capacitor mobile wrapper with push.
- Multi-node workers, Arrow Flight service option, ClickHouse/Cognite/SiteWise connectors.
- SOC 2 readiness pack; offline licensing.

## Repository layout

As of sprint 6. Directories marked *planned* arrive with the sprint in brackets.

```
Tabayyun/
├── core/                    # Rust workspace
│   ├── tabayyun-core/       # library: frame, profile, checks, score, downsample, synth
│   │                        #   (cache [S7], repair [S11] planned)
│   ├── tabayyun-py/         # PyO3 bindings → wheel `tabayyun_core`
│   └── tabayyun-cli/        # `tabayyun` binary: offline checks, CSV/Parquet in, JSON out
├── api/                     # Python: FastAPI app and the Procrastinate worker (one image)
│   ├── src/tabayyun/        # routers, services, db (models, packaged migrations), jobs
│   └── tests/               # unit tests; tests/db/ runs against Postgres
├── web/                     # Vite + React 19 + TanStack SPA, Vitest tests in src/__tests__/
├── deploy/                  # compose bundles, Caddyfile, CapRover definitions, deploy README
│                            #   (Keycloak realm [S8], Helm and air-gap scripts [S13] planned)
├── docs/                    # research, architecture, ADRs, checks, specs, roadmap, assets
├── TASKS.md                 # spec backlog and session notes
└── (planned) checks/        # check manifests for plugin checks [S13]
```
