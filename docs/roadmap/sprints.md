# Sprint plan — weekly sprints, Sunday to Saturday

*Planned 2026-09-21. Sprints are one week because the build budget resets every Saturday.
A sprint's stories are ordered by priority: when the budget reaches ~90 % the remaining
stories roll into the next sprint unchanged, nothing is squeezed in. One branch per sprint
(`sprint/NN-topic`), one PR, CodeRabbit review, merge by the owner, release from `main`
deploys to CapRover.*

The weekly plan supersedes the earlier duration estimates in `roadmap.md` (12, 8–10 and
8–12 weeks): R1 keeps its length, R2 and R3 are compressed to five and four sprints because
the checks, connectors and screens they need are built on the R1 foundation.

Sizing rule of thumb from sprints 1–4: one sprint fits roughly *one* of the following:
six checks with tests, or one vertical slice through API + web + persistence, or one
connector with its screens. Research notes are cheap and go last in a sprint if budget
remains. Priorities: **M** must (sprint fails without it), **S** should, **C** could.

Definition of done for every story: tests (Rust unit or pytest or vitest), lint clean, docs
touched (`catalogue.md`, ADR when a design decision is made, roadmap progress log), and,
for user-facing stories, a screenshot or curl transcript in the PR.

## Milestones

| Milestone | Sprints | Exit criteria |
|---|---|---|
| **R1 core, release 0.1** | 5–13 (until 21 Nov 2026) | 30 checks; persisted runs, findings, scores; PI Web API and OPC UA connectors; Google login, workspace RBAC; overview, series detail, findings inbox; alerts; corrections with lineage and a corrected layer; share links; pilot install on Hetzner; ≥95 % of injected faults detected at ≤2 % false positives per check |
| **R2 domain depth** | 14–18 (until 26 Dec 2026) | metering, PV, wind, grid, oil and gas packs; RepairFlows with regulatory estimation; write-back; public benchmark corpus |
| **R3 platform, release 1.0** | 19–22 (until 23 Jan 2027) | enterprise SSO and SCIM, API tokens, embeds, fleet baselines, multi-node workers, more connectors, SOC 2 pack, licensing |

## Sprint 5 — 20 to 26 Sep 2026 (this week, ~10 % budget left)

Goal: land what is open, plan, no new feature work.

| ID | Story | Prio | Done when |
|---|---|---|---|
| S5-1 | Merge `fix/pr6-review-followup` (web image copies `pnpm-workspace.yaml`, doc corrections) | M | PR merged, release green |
| S5-2 | This sprint plan merged and linked from the roadmap | M | `docs/roadmap/sprints.md` on `main` |
| S5-3 | CapRover: confirm real session secret and db password in the api app, delete the sprint/04 and fix branches | M | owner confirms |
| S5-4 | Upload page exposes `physical_min` / `physical_max` (two inputs, passed as form fields) | C | range checks run from the browser |

## Sprint 6 — 27 Sep to 3 Oct — persistence and jobs

Goal: a run leaves a trace. Every upload becomes a `Run` with persisted findings, metrics
and scores in Postgres; a worker executes it.

| ID | Story | Prio | Done when |
|---|---|---|---|
| S6-1 | SQLAlchemy 2 async models + Alembic: `orgs`, `workspaces`, `sources`, `series`, `datasets`, `runs`, `findings`, `metrics`, `scores`, `coverage`; Timescale hypertables for findings/metrics/scores with an "Apache-2-only" switch (ADR-0003) | M | `alembic upgrade head` on the CapRover db; models tested against a Postgres service in CI |
| S6-2 | Procrastinate worker container and `run_suite` task; API enqueues, worker calls the core, persists results; run status polling endpoint | M | upload returns a `run_id`; `GET /api/runs/{id}` shows status and counts; worker deployed as a fourth CapRover app |
| S6-3 | Findings persistence with open-finding deduplication on overlapping windows; `GET /api/findings` with filters (series, check, severity, status) and pagination | M | duplicate upload does not double findings |
| S6-4 | Series and source records created from uploads (`source.type = upload`), series metadata (unit, limits, kind) editable via `PATCH /api/series/{id}` | S | metadata survives and feeds the next run |
| S6-5 | Web: runs list, run report page (score tiles, findings table, evidence expand) reading persisted data | S | page works after a browser refresh |
| S6-6 | Docs: ADR-0012 run and finding lifecycle (statuses open/acked/resolved/muted, dedup rule) | S | ADR merged |

## Sprint 7 — 4 to 10 Oct — Parquet cache and cross-series checks

Goal: data is cached once and checks can see more than one series.

| ID | Story | Prio | Done when |
|---|---|---|---|
| S7-1 | Rust `cache` module: Parquet writer (append-only, sorted `(series_id, ts)`, zstd, ~1M-row groups) and reader with tag and time predicate push-down; layout `cache/{layer}/{source}/{bucket}/{yyyy}/{mm}/` | M | round-trip tests; 10M rows write + read benchmark |
| S7-2 | Coverage table and incremental fetch: a run reads from the cache and only fetches missing ranges | M | second run over the same window fetches nothing |
| S7-3 | Core multi-series API: `run_checks_multi(frames, related, configs)` with Arrow in/out through the wheel | M | bindings test with two series |
| S7-4 | Check 22 `tby.correlation_break` (rolling robust correlation vs baseline on related series) | M | synthetic-fault tests; 3W pair (P-PDG, T-PDG) |
| S7-5 | Check 23 `tby.redundant_disagreement` (learned spread + margin, explicit tolerance override) | M | synthetic 2oo3 tests |
| S7-6 | Check 24 `tby.balance_residual` (balance group definition, propagated uncertainty, suspect member) | S | synthetic inlet/outlet tests |
| S7-7 | Check 21 `tby.seasonality_break` (daily/weekly strength vs profile) | S | OPSD load positive, wind negative |
| S7-8 | Datasets API: explicit series selection and query selection, window policy; related-series suggestion job (correlation over cache) | C | dataset feeds a run |

## Sprint 8 — 11 to 17 Oct — login, tenants and RBAC

Goal: a user logs in with Google, lands in a workspace, and sees only what they may.

| ID | Story | Prio | Done when |
|---|---|---|---|
| S8-1 | Keycloak in the compose bundle and as a CapRover app with a `tabayyun` realm export; Google as brokered IdP | M | login page reachable, Google login works |
| S8-2 | OIDC Authorization Code + PKCE BFF in FastAPI: server-side session, `__Host-` cookie, CSRF header, back-channel logout, `require_secrets_in_prod` covers OIDC | M | pytest with a mocked IdP; prod refuses placeholders |
| S8-3 | `authz` package: role hierarchy (org admin, workspace admin, editor, viewer), `authorize()`, `visible_ids()`, RLS via `SET LOCAL app.org_id` (ADR-0007) | M | RLS tests prove cross-workspace reads fail |
| S8-4 | Tenant APIs: organisations, workspaces, members, teams, invitations; audit events for every mutation | M | admin can invite by email, invitee joins with Google |
| S8-5 | Web: login route, org and workspace picker, session refresh, admin panel v1 (workspaces, members, roles, teams, invitations, audit log viewer) | S | all admin actions possible from the browser |
| S8-6 | Security baseline pass 1: headers, cookie flags, rate limit on auth routes, dependency audit in CI | S | checklist items ticked in `02-security-baseline.md` |

## Sprint 9 — 18 to 24 Oct — connectors and the series screen

Goal: real historians feed Tabayyun and a series has a home page.

| ID | Story | Prio | Done when |
|---|---|---|---|
| S9-1 | Connector framework: `fetch_window` job per source with batching, rate limits, retries, health; credentials in a secret store reference, write-only in the API | M | framework tests with a fake connector |
| S9-2 | PI Web API connector: point search, recorded values with quality mapping, PI AF metadata import (unit, limits, `ExcDev`/`CompDev`/`CompMax`) | M | integration test against a recorded fixture; docs |
| S9-3 | OPC UA connector (asyncua): browse address space, history read, StatusCode mapping incl. `Good_LocalOverride` and `Good_Clamped` to uncertain | M | test against `opcua-asyncio` server fixture |
| S9-4 | Web: sources list and detail (health, last fetch, metadata import), series catalogue with search and filters (source, unit, score, kind) | S | pages backed by the APIs |
| S9-5 | Web: series detail with uPlot chart, M4 downsampled endpoint, findings as shaded windows, quality rug, baseline band; profile and metadata side panel; keyboard range inputs | M | 1M-point series renders under 500 ms after the first load |
| S9-6 | CSV/Parquet file source that watches a directory (air-gapped sites) | C | files dropped in a folder appear as series |

## Sprint 10 — 25 to 31 Oct — suites, triage, alerts, overview

Goal: quality is monitored continuously, not run by hand.

| ID | Story | Prio | Done when |
|---|---|---|---|
| S10-1 | Check suites: CRUD, cron schedule, per-check thresholds with "why this threshold" (metadata / auto-baseline / override), run history | M | scheduled suite produces runs without a user |
| S10-2 | Baseline profile job (`profile_series`, trailing 28 days, excludes Bad quality and open critical findings), stored and versioned | M | thresholds visibly change after a profile refresh |
| S10-3 | Findings inbox: virtualised table, saved filters, group by check/series/asset, ack, mute with expiry and reason, resolve, assign, CSV export, "false positive" feedback | M | all actions persisted and audited |
| S10-4 | SSE stream for `run.completed` and `finding.created`; web updates live | S | inbox updates without refresh |
| S10-5 | Alerts: rules on findings and scores, channels email and webhook, throttling, delivery job | M | an alert email arrives for a critical finding |
| S10-6 | Overview: score tiles per dimension with 30-day sparklines, worst 5 % series, findings by severity over time, connector health strip; filters in URL | S | overview loads in under 1 s for 10k series |

## Sprint 11 — 1 to 7 Nov — corrections v1

Goal: a user fixes a window and sees the corrected layer re-scored, with lineage
(ADR-0010).

| ID | Story | Prio | Done when |
|---|---|---|---|
| S11-1 | Rust `repair` module: `mask`, `clamp`, `dedupe`, `impute.linear`, `impute.like_day`, `align.resample`, `transform.affine`; each returns new values plus a lineage frame | M | unit tests per op; property test "raw never changes" |
| S11-2 | Corrections API: propose, approve, reject; `Correction` and `CorrectedRange` tables; corrected-layer versions written to the cache (`corrected/v{n}`) | M | version n+1 appears, raw untouched |
| S11-3 | Re-scoring of the corrected layer; reads name a layer (`raw`, `corrected@latest`, `corrected@v3`), default raw | M | "raw 61 → corrected 94" in the API |
| S11-4 | Web: select window on the chart → choose operation → preview raw vs corrected diff → propose; approval per workspace policy | M | full flow from the browser |
| S11-5 | Publish targets: Parquet export and SQL table; `publish_correction` job | S | corrected series lands in a Parquet file |
| S11-6 | Audit and feedback: accepted/rejected corrections logged, threshold-suggestion job skeleton | C | job produces a suggestion row |

## Sprint 12 — 8 to 14 Nov — sharing and the energy pack

Goal: results can be shared safely; the catalogue reaches 30 of 30.

| ID | Story | Prio | Done when |
|---|---|---|---|
| S12-1 | Shares: grant to user or team with role and expiry; link shares with hashed token, locked scope, optional password, revoke; `expire_shares` job | M | link viewer works without login and only shows the scope |
| S12-2 | Web: share dialog (People, Link tabs) on series, findings and overview; link viewer page with scope banner; share inventory in admin | M | screenshots in PR |
| S12-3 | Checks 25 and 26: `energy.metering.register_reconciliation`, `energy.metering.usage_plausibility` | M | UBP/AEMO/Elexon presets tested |
| S12-4 | Checks 27 and 28: `energy.pv.irradiance_limits` (BSRN), `energy.pv.time_shift` (solar-noon offset) with solar position in Rust | M | PVDAQ sample tests |
| S12-5 | Checks 29 and 30: `energy.pv.clipping`, `energy.wind.power_curve_outlier` | S | synthetic tests; Kelmarsh sample |
| S12-6 | Mobile layouts and PWA manifest, install prompt, offline shell | C | Lighthouse PWA pass |

## Sprint 13 — 15 to 21 Nov — hardening and release 0.1

Goal: a pilot site can install and trust it.

| ID | Story | Prio | Done when |
|---|---|---|---|
| S13-1 | Plugin host: sandboxed Python checks with the same evidence and scoring model, YAML check DSL, manifest registry (ADR-0009) | S | example plugin runs in a dedicated worker |
| S13-2 | Reports: scheduled quality report, CSV and PDF export, shareable | S | report emailed on schedule |
| S13-3 | Performance run: 100k tags × 1 year synthetic on 8 cores in under one hour; criterion benchmarks per kernel; profiling fixes | M | numbers in `docs/roadmap/roadmap.md` |
| S13-4 | Security: ASVS L2 self-assessment, threat model, pen-test prep, SBOM and image signing verified, pip-audit and cargo-audit gates | M | checklist complete, findings fixed |
| S13-5 | Install paths: Helm chart draft, air-gap tarball script, backup and restore runbook (Postgres dump + cache copy) | S | restore rehearsal on a fresh server |
| S13-6 | Detection benchmark: synthetic corpus run, ≥95 % detection at ≤2 % false positives per check, published table | M | table in docs |
| S13-7 | Release 0.1: tag, changelog, GHCR images, pilot install on Hetzner validated end to end | M | `v0.1.0` tag, release notes |

## Sprint 14 — 22 to 28 Nov — metering pack and RepairFlows

| ID | Story | Prio | Done when |
|---|---|---|---|
| S14-1 | VEE rule presets (UBP, AEMO, Elexon/MHHS) as suite templates with published thresholds | M | preset selectable per dataset |
| S14-2 | Estimation methods with substitution types: linear ≤2 h, like-day, average like-day, previous-year like-day, zero; precedence order per preset | M | corrections carry the substitution code |
| S14-3 | DST interval-count rule (92/96/100), estimated-share reporting per meter per month | S | spring-forward and fall-back days pass; monthly estimated share per meter in the API |
| S14-4 | RepairFlows: scheduled block pipelines (filter → impute → align → publish) with approval policies, run after suite | M | a flow repairs gaps nightly |
| S14-5 | `impute.seasonal`, `impute.kalman` in the Rust repair module | S | unit tests recover an injected gap within 5 % RMSE on a daily-cycle series |

## Sprint 15 — 29 Nov to 5 Dec — PV and wind packs

| ID | Story | Prio | Done when |
|---|---|---|---|
| S15-1 | PV: clear-sky and daily insolation limits, capacity data shifts, soiling and pyranometer drift vs reference, PR sensitivity to gaps | M | PVDAQ-based tests |
| S15-2 | Wind: IEC 61400-12-1 filtering presets, icing signature, implausible min/max/std, event-log correlation | M | Kelmarsh/Penmanshiel tests |
| S15-3 | Physics-aware imputation for PV (clear-sky scaled) and wind (power-curve) | S | imputed day within 10 % of the withheld PVDAQ actual |

## Sprint 16 — 6 to 12 Dec — oil and gas pack 1 (research 05)

| ID | Story | Prio | Done when |
|---|---|---|---|
| S16-1 | Companion state series: shut-in and valve-state masking for flatline, non-negative and range findings | M | 3W DHSV-closure instance no longer reports the closed-in period as stuck |
| S16-2 | PI `ExcDev`/`CompDev`/`CompMax` from the connector into series metadata; compression-aware `flatline`, `interpolation_artifacts`, `resolution_loss` | M | compressed 3W tags produce one informational finding per window |
| S16-3 | `interpolation_artifacts` aggregated into episodes per ADR-0011 | M | ≤ 5 findings per 3W series |
| S16-4 | Quality-code sub-findings for OPC `Good_LocalOverride`, `Good_Clamped`, `Uncertain_*` | S | asyncua fixture with overridden node yields a `quality_flags` sub-finding |
| S16-5 | Choke-vs-flow consistency rule; P/T consistency pairs as `correlation_break` presets | C | 3W instance with choke open and zero gas-lift flow is flagged |

## Sprint 17 — 13 to 19 Dec — oil and gas pack 2

| ID | Story | Prio | Done when |
|---|---|---|---|
| S17-1 | `redundant_disagreement` SIS discrepancy override and growing-deviation signature | M | synthetic drifting channel flagged before it crosses the trip limit |
| S17-2 | `balance_residual` with VDI 2048-style propagated uncertainty and suspect ranking; `reconcile.balance` repair op | M | pipeline inlet/outlet fixture |
| S17-3 | Allocation imbalance and well-test-vs-MPFM checks with contractual tolerance parameters | S | synthetic field with a 3 % imbalance flagged at a 2 % tolerance, passes at 5 % |
| S17-4 | Meter-factor drift from proving history; alarm-rate, chattering and stale KPIs on event series (EEMUA 191 / ISA-18.2) | S | proving series with 0.06 % repeatability flagged; alarm flood (>10 in 10 min) detected on a synthetic event log |
| S17-5 | Timestamp skew across RTUs before balance checks | C | a 30 s skew between inlet and outlet is reported and no phantom imbalance results |

## Sprint 18 — 20 to 26 Dec — grid pack, write-back, benchmark corpus (holiday week, half capacity)

| ID | Story | Prio | Done when |
|---|---|---|---|
| S18-1 | Grid: PMU STAT-word decoding, NASPI completeness attributes, market-interval counts | S | C37.118 fixture decoded; a day with 95 instead of 96 intervals flagged |
| S18-2 | PI and OPC write-back publish targets to separate tags | S | corrected values land in the mapped tag; a test proves the source tag is never written |
| S18-3 | Threshold suggestions from false-positive feedback | S | three false-positive marks on one check produce a suggestion the editor can accept |
| S18-4 | Public labelled benchmark corpus (synthetic + PVDAQ, Kelmarsh/Penmanshiel, OPSD, Elia, 3W) under CC-BY with expected findings | C | repository published |

## Sprint 19 — 27 Dec to 2 Jan 2027 — enterprise identity

| ID | Story | Prio | Done when |
|---|---|---|---|
| S19-1 | Per-organisation OIDC/SAML connections through Keycloak, group-to-role mapping | M | Entra test tenant login |
| S19-2 | API tokens (scoped, hashed, expiring) and service accounts | M | token with viewer scope cannot mutate; expired token rejected; only the hash is stored |
| S19-3 | Embeds with signed JWT and locked scope | S | embedded series chart renders on an external page and rejects a tampered JWT |
| S19-4 | SCIM provisioning after Entra/Okta preview testing | C | user created and deprovisioned from an Entra test tenant |

## Sprint 20 — 3 to 9 Jan — fleet baselines and scale

| ID | Story | Prio | Done when |
|---|---|---|---|
| S20-1 | Fleet baselines: template-level thresholds by asset type, override inheritance | M | a template change propagates to all series of the type unless overridden; "why this threshold" shows the source |
| S20-2 | Operating-mode segmentation (running, idle, maintenance) feeding all adaptive checks | M | profiles and findings are computed per mode; idle periods raise no operational-range findings |
| S20-3 | Multi-node workers, queue partitioning, Arrow Flight service option | S | two worker nodes share a queue without duplicate runs; Flight endpoint streams a series |

## Sprint 21 — 10 to 16 Jan — connectors and mobile

| ID | Story | Prio | Done when |
|---|---|---|---|
| S21-1 | ClickHouse, Cognite Data Fusion and AWS SiteWise connectors | M | each connector passes the framework contract tests against a recorded fixture |
| S21-2 | Capacitor mobile wrapper with push notifications for alerts | S | Android and iOS builds receive a push for a critical finding |
| S21-3 | Explorer: DataFusion SQL over the cache, read-only with timeouts | C | a write statement is rejected; a 10 s query is cancelled |

## Sprint 22 — 17 to 23 Jan — compliance and release 1.0

| ID | Story | Prio | Done when |
|---|---|---|---|
| S22-1 | SOC 2 readiness pack: policies, audit-log retention, access reviews | M | policy documents in `docs/compliance/`; retention job and access-review export exist |
| S22-2 | Offline licensing and entitlements | M | signed licence file gates features without network access; expiry warns 30 days ahead |
| S22-3 | Release 1.0: docs site, upgrade guide, changelog | M | `v1.0.0` |

## Working agreement

- Sunday: branch `sprint/NN-topic` from `main`, stories in priority order, must-haves first.
- Every push runs CI; every PR gets `@coderabbitai full review`; findings are fixed before
  the owner merges. Merges deploy to CapRover automatically.
- Budget check at ~70 % and ~90 %: at 70 % stop starting new stories that need research; at
  90 % stop, push, write the progress-log entry, and roll the rest forward.
- Rolled-over stories keep their ID and move to the top of the next sprint.
