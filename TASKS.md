# Tabayyun — Backlog

Status legend: `[ ]` open, `[~]` in progress, `[x]` done. One spec per session; mark it here
and add one line per non-obvious decision below the entry (these are the session notes the
next session reads). Sprint dates and priorities live in `docs/roadmap/sprints.md`; the
story ids there (`S6-1`) map to specs here.

## Sprints 1–5 (done before specs existed)

- [x] Research 01–05, vision, domain model, architecture, authz design, frontend design,
      security baseline, roadmap, ADRs 0001–0012, sprint plan.
- [x] Rust core: 20 of 30 checks, profile, scoring v2, downsampling, synth, CLI, PyO3 wheel.
- [x] API: FastAPI skeleton, `POST /api/checks/run` on an uploaded CSV, prod secret guard.
- [x] Web: upload page with score tiles and findings.
- [x] CI (rust core, python bindings, python api, web), release images to GHCR, CapRover
      deploy job, live at the Hetzner CapRover instance.
- [x] Finding aggregation into episodes (ADR-0011), frozen-tag rule, historian timestamps,
      test datasets (UCI, OPSD, Petrobras 3W) with measured expectations.
      - Sprint 5 lesson: a Dependabot Node major bump removed Corepack from the web image;
        Node majors are now ignored by Dependabot and taken by hand on the LTS line.

## Sprint 6 — persistence and jobs (27 Sep – 3 Oct 2026)

- [ ] **001 Persistence schema and migrations** — `docs/specs/001-persistence-schema.md`
      SQLAlchemy 2 async models, Alembic, hypertables with the Apache-2-only switch, seed
      org/workspace, schema-revision guard at startup, CI Postgres service.
- [ ] **002 Runs and the worker** — `docs/specs/002-runs-and-worker.md`
      `POST /api/runs` + Procrastinate worker, uploads in Postgres, inline-jobs mode for
      tests, stale-run reaper, worker container and CapRover app.
- [ ] **003 Findings persistence, dedup and API** — `docs/specs/003-findings-persistence.md`
      Persist scores/metrics/findings, overlap dedup with occurrences, status transitions,
      list/filter/paginate, ADR-0013 lifecycle.
- [ ] **004 Series and sources from uploads** — `docs/specs/004-series-and-sources.md`
      `Uploads` source, series upsert, metadata precedence, `PATCH /api/series/{id}`.
- [ ] **005 Runs list and report in the web app** — `docs/specs/005-runs-web.md`
      `/runs`, `/runs/new` (with physical limits), `/runs/:id` with polling and evidence.

## Sprint 7 — Parquet cache and cross-series checks (4 – 10 Oct)

Specs to write at sprint start (stories S7-1 … S7-8 in `docs/roadmap/sprints.md`; 007 is
taken by the RLS spec that specs 001–004 already reference):
- [ ] 006 Parquet cache and coverage
- [ ] 008 Multi-series core API and datasets
- [ ] 009 `tby.correlation_break`
- [ ] 010 `tby.redundant_disagreement`
- [ ] 011 `tby.balance_residual`
- [ ] 012 `tby.seasonality_break`

## Sprint 8 — login, tenants and RBAC (11 – 17 Oct)

- [ ] 007 Row-level security, memberships and roles (ADR-0007)
- [ ] 013 Keycloak realm and OIDC BFF
- [ ] 014 Tenant APIs and admin panel v1
- [ ] 015 Security baseline pass 1

## Later sprints

Stories S9-1 onwards in `docs/roadmap/sprints.md` become specs 016+ when their sprint
starts; the plan is the backlog until then.
