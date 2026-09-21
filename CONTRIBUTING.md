# Contributing to Tabayyun

Thanks for helping verify industrial time series. This page covers the workflow; the design
lives in `docs/` and is the source of truth: read `docs/architecture/03-system-architecture.md`,
`docs/checks/00-check-specification.md` and the ADRs in `docs/adr/` before changing behaviour.

## Ground rules

- **Design first.** A change that deviates from a documented decision needs a new ADR
  (copy `docs/adr/0000-adr-template.md`), not a silent workaround.
- **Checks are pure.** No I/O inside a check. Every check ships with synthetic-fault unit
  tests, evidence JSON and a plain-language summary (`docs/checks/00-check-specification.md`).
- **Never overwrite raw data.** Corrections are versioned layers (ADR-0010); reads name a layer.
- **No secrets** in code, config, fixtures or tests. Environment variables only.
- Timestamps are `i64` nanoseconds since epoch UTC; NaN means null; quality is normalised to
  good / uncertain / bad / estimated.
- Arrow is pinned to the major version `pyo3-arrow` supports; bump both together.

## Development setup

| Part | Toolchain | Commands |
|---|---|---|
| `core/` | Rust 1.88+ | `cargo test && cargo clippy --all-targets -- -D warnings && cargo fmt --check` |
| `core/tabayyun-py/` | maturin | `VIRTUAL_ENV=../../api/.venv ../../api/.venv/bin/maturin develop --release && ../../api/.venv/bin/pytest -q` |
| `api/` | Python 3.11+, uv | `uv sync --extra dev && uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy` |
| `web/` | Node 22, pnpm 10 | `pnpm install --frozen-lockfile && pnpm lint && pnpm build` |

`make lint` and `make test` run everything; `make demo` runs the checks on a synthetic faulty
series; `make dev-infra` starts Postgres+TimescaleDB and Keycloak via docker compose.

## Workflow

1. Open or pick an issue. Label it with an `area:` and, if it is a check, `type: check`.
   Anything that changes a decision gets `needs: design` and an ADR draft.
2. Branch from `main`: `feat/<short-topic>`, `fix/<short-topic>`, `docs/<short-topic>`.
3. Commit in small, logical steps with semantic messages:
   `feat(core): ...`, `fix(api): ...`, `docs: ...`, `refactor(web): ...`, `chore(ci): ...`,
   `test(core): ...`. Scope is the directory or check id. No debug code, no `WIP` commits on
   the final branch (squash locally if needed).
4. Open a pull request against `main` and fill in the template. Link the issue with
   `Closes #n`. Keep PRs reviewable: one concern per PR, under ~500 changed lines where
   possible.
5. CI must be green (`rust core`, `python bindings`, `python api`, `web`). Review threads
   must be resolved before merge. Merge with a merge commit or squash; rebase-merge is off.

## Adding a check

1. Pick the next id from `docs/checks/catalogue.md`; do not invent ids outside the catalogue
   without a "Check proposal" issue.
2. Implement in `core/tabayyun-core/src/checks/<name>.rs`, register it, and add the fault to
   `synth` so the test can inject it.
3. Tests: at least one clean series producing no finding and one injected fault producing the
   expected finding with sensible evidence. Property tests are welcome.
4. Update the catalogue status column, the README list, and any threshold rationale.

## Licence of contributions

Tabayyun is licensed under Apache-2.0 (`LICENSE`, ADR-0012). By submitting a contribution you
agree it is licensed under the same terms (inbound = outbound). There is no CLA. Do not
contribute code or data you are not entitled to license this way, and never real customer data.

## Repository settings (maintainers)

Protection for `main` is defined as a ruleset in `.github/rulesets/protect-main.json`:
pull request required, the four CI jobs required, review threads resolved, no force-push,
no deletion, no bypass. Committing the file does not enforce anything: a repository admin
has to import it once (and re-import after editing it) under **Settings → Rules → Rulesets →
New ruleset → Import a ruleset**, or with the GitHub CLI:

```bash
gh api -X POST repos/thedatadudech/Tabayyun/rulesets --input .github/rulesets/protect-main.json
```

Until the ruleset is active, the CI and review-thread gates above are convention, not
enforcement. Recommended repository settings (Settings → General): automatically delete head branches,
allow auto-merge, always suggest updating pull request branches, rebase merging off.
Settings → Code security: private vulnerability reporting, Dependabot alerts and security
updates, secret scanning with push protection.
