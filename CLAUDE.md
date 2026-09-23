# Tabayyun — working notes for Claude Code

Self-hostable time-series data-quality platform. Read `docs/` before changing design:
`docs/checks/catalogue.md` (the 30 checks), `docs/architecture/03-system-architecture.md`,
`docs/adr/` (decisions; add a new ADR rather than silently deviating).

## Session protocol (spec-driven, as in Thawr)

1. One spec per session. The prompt names it (`Implement docs/specs/NNN-name.md. Plan first.`).
   Read this file, `docs/architecture/03-system-architecture.md` and that one spec plus the
   specs it references; do not read all specs.
2. Present the plan, wait for approval, then implement. Deviating from a spec, an ADR or a
   fixed architecture decision needs a question first, or a new ADR.
3. `make lint` and `make test` (or the relevant subset) before every commit; semantic commits,
   one logical change each; a spec may take several commits.
4. When the spec's acceptance criteria are met, tick them in the spec, mark it done in
   `TASKS.md` and add one line per non-obvious decision under its entry.
5. Do not start the next spec in the same session. Do not refactor code the spec does not touch.
6. A story in `docs/roadmap/sprints.md` gets its spec (copy `docs/specs/000-template.md`)
   before any code; a spec that turns out wrong is edited in the same PR, with the reason.

## Layout
- `core/` Rust workspace: `tabayyun-core` (frame, profile, checks, score, downsample, synth), `tabayyun-cli` (`tabayyun` binary), `tabayyun-py` (PyO3 wheel `tabayyun_core`, Arrow PyCapsule in/out).
- `api/` Python 3.11+ FastAPI (uv). `src/tabayyun/`, tests in `tests/`.
- `web/` Vite + React 19 + TanStack + Tailwind v4 SPA (pnpm).
- `deploy/` compose bundles, Caddyfile, deployment README; `api/Dockerfile`, `web/Dockerfile`; `.github/workflows/release.yml` publishes images to GHCR. `docs/` design and research.

## Commands
- `make lint` / `make test` run everything. `make demo` runs the checks on a synthetic faulty series.
- Rust: `cd core && cargo test && cargo clippy --all-targets -- -D warnings && cargo fmt --check`
- Python: `cd api && uv sync --extra dev && uv run pytest -q && uv run ruff check . && uv run mypy` (uv builds the core wheel from `../core/tabayyun-py`; Rust toolchain required). Database tests run when `TABAYYUN_TEST_DATABASE_URL` is set (`make dev-infra` provides one); `make db-upgrade` / `make db-revision m="..."` for migrations; `make worker-dev` runs the job worker (or `TABAYYUN_INLINE_JOBS=true`).
- Bindings: `cd core/tabayyun-py && VIRTUAL_ENV=../../api/.venv ../../api/.venv/bin/maturin develop --release && ../../api/.venv/bin/pytest -q`
- Web: `cd web && pnpm install --frozen-lockfile && pnpm lint && pnpm build`

## Conventions
- Checks are pure (no I/O); every check has synthetic-fault unit tests; every finding has evidence JSON and a plain-language summary.
- Timestamps are `i64` ns since epoch UTC. NaN = null. Quality is normalised to good/uncertain/bad/estimated.
- Arrow is pinned to the major version pyo3-arrow supports (currently 59); bump both together.
- Never overwrite raw data; corrections are versioned layers (ADR-0010). Reads name a layer explicitly.
- No secrets in code or config; env vars only; prod refuses placeholders.
- Semantic commit messages (`feat:`, `fix:`, `docs:`, `refactor:`, `chore:`, `test:`).
- Diagrams are Mermaid in Markdown (`flowchart`, `sequenceDiagram`, `stateDiagram-v2`,
  `gantt`, `erDiagram`), not ASCII art, so GitHub renders them; check a new one renders
  before committing.
- Licence is Apache-2.0 (ADR-0012). Workflow, PR checklist and branch rules: `CONTRIBUTING.md`.
- Backlog and session notes: `TASKS.md`. Specs: `docs/specs/`. Sprint plan: `docs/roadmap/sprints.md`.
