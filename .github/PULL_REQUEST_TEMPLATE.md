## What

<!-- One or two sentences: what changes and why. Link the issue: Closes #123 -->

## Checks

- [ ] Implements a spec in `docs/specs/` (linked above); its acceptance criteria are ticked and `TASKS.md` is updated
- [ ] `make lint` and `make test` pass locally (or the relevant subset: `cargo`, `uv`, `pnpm`)
- [ ] New or changed checks have synthetic-fault unit tests, evidence JSON and a plain-language summary
- [ ] Design deviations are recorded as a new ADR in `docs/adr/` (not silently)
- [ ] Docs updated (`docs/checks/catalogue.md` status, README, `deploy/README.md`) where behaviour changed
- [ ] No secrets, credentials or placeholder values in code or config
- [ ] Commit messages follow `type(scope): summary` (feat, fix, docs, refactor, chore, test)

## Notes for the reviewer

<!-- Anything non-obvious: trade-offs, follow-ups, how to try it. -->
