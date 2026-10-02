# ADR-0017: Auto-merge, published releases with an email, and release reviews

- **Status:** Accepted
- **Date:** 2026-10-02
- **Deciders:** owner

## Context

Every pull request waited for the owner to press "Merge", although the owner reviews outcomes
on staging, not diffs. `main` deploys to staging on every merge, and production only runs
what the owner approves in `promote.yml` (ADR-0016). So the merge click added waiting time,
not safety. The owner wants to hear by email when something is released, and to see bigger
releases in a presentation with a calendar slot, instead of following pull requests. No
release had been published yet: no tags, no GitHub releases, an `[Unreleased]` changelog.

The owner decided on 2 Oct 2026:
- every pull request merges automatically once CI is green and no blocking review finding is
  open;
- an email for each published release;
- a stable minor or major release counts as a bigger release;
- for it, slides and a one-hour calendar slot.

## Decision

- **Auto-merge for every pull request.** A pull request merges itself once the `protect-main`
  ruleset is met:
  - the four CI jobs are green;
  - CodeRabbit's review is complete (its `CodeRabbit` commit status is required, and only
    from CodeRabbit's GitHub App, integration 347564, so nothing else can post it);
  - every review thread is resolved.

  `automerge.yml` enables GitHub's auto-merge (merge commit) on every non-draft pull request
  from a branch of this repository, Dependabot's included, but only while `main` requires the
  `CodeRabbit` check, so it stays off during an outage too. It uses `AUTOMATION_TOKEN`, a
  fine-grained token of the owner. GitHub starts no workflow for a merge made through
  `GITHUB_TOKEN`, so a merge through it would never deploy. The workflow refuses while `main`
  requires no status checks, because GitHub would then merge at once. Pull requests from
  forks are left to a person: their code has had no trusted look, and auto-merge would
  deploy it to staging. Pull requests that need an owner step before they may deploy (a
  setting, a secret, a Keycloak change) stay drafts until it is done.
- **Releases.** `cut-release.yml` (run on `main` by Claude Code or the owner) publishes a
  version:
  - **patch:** a finished spec or a set of fixes;
  - **minor:** a finished sprint;
  - **major:** a product milestone the owner names;
  - **release candidate:** `vX.Y.Z-rc.N`, optional.

  The job checks two things first: main changed since the last release, and `release.yml`
  succeeded for the commit, so staging runs it. It then pushes the `v*` tag with the owner's
  token (only organisation admins may create those tags, and only such a push starts
  `release.yml` for the versioned images). Finally it creates the GitHub release with notes
  from the merged pull requests. Before 1.0 a minor version may break APIs (`CHANGELOG.md`).
- **Release email.** The same job emails `SHIP_MAIL_TO` the version, its pull requests with
  links and the release notes link, over SMTP with a Google Workspace app password
  (`SMTP_USERNAME`, `SMTP_PASSWORD`). Without them it only logs a notice; a mail error never
  fails a release. Merges and deploys send no mail: a failed workflow already triggers
  GitHub's own notification to the owner, as whose account merges and releases run.
- **Release review.** For a stable minor or major release, Claude Code makes a slide deck of
  what shipped and books a one-hour review in the owner's Google calendar (`CLAUDE.md`,
  "Shipping").
- **Production stays gated.** Promotion still needs the owner's approval of a `promote.yml`
  run (ADR-0016).

## Alternatives considered

| Option | Pros | Cons | Why not |
|---|---|---|---|
| Owner merges every PR | a human look before staging | waiting time; the owner judges results on staging anyway | the owner chose auto-merge |
| Enable auto-merge with `GITHUB_TOKEN` | no extra secret | its merges start no workflows, so nothing would deploy | the owner's token |
| Auto-merge for forks too | nothing waits | untrusted code reaches staging without a person | forks wait for a person |
| An email per deploy | hears of every change | many mails, most for small fixes | the owner chose one per release |
| release-please | changelog and version bumps from commits | its PRs, opened with `GITHUB_TOKEN`, start no CI, so they can never merge; one release per merge | releases are cut on purpose |
| A mail action from the marketplace | less code | a third-party action sees the SMTP password | a short standard-library script |

## Consequences

- A change reaches staging without a person looking at the diff. The gates are CI,
  CodeRabbit, resolved threads and Claude Code's own checks before it pushes. Production
  keeps its human approval. Dependabot majors merge once CI passes, so CI coverage of the
  web build and tests carries more weight.
- A CodeRabbit outage blocks every merge, manual ones included, because the required status
  never arrives and the ruleset has no bypass. The owner first turns off auto-merge on the
  open pull requests (they would otherwise merge unreviewed), then removes `CodeRabbit` from
  `protect-main`'s required checks, merges what is needed by hand, adds the check back and
  re-enables auto-merge (CONTRIBUTING.md, "Repository settings"). While the check is out,
  `automerge.yml` refuses to enable auto-merge, so a new push cannot merge unreviewed.
- `AUTOMATION_TOKEN` can push to the repository as the owner. It is scoped to this one
  repository, expires, and is renewed by the owner. It is read only by workflow files on
  `main`.
- The version strings in the code (`0.1.0` in the API, the web package and the crates) are
  not bumped by a release yet; `/api/version` keeps reporting the commit.
- Owner set-up, once: allow auto-merge, activate the rulesets, and add `AUTOMATION_TOKEN`, the
  SMTP secrets and `SHIP_MAIL_TO` (`deploy/caprover.md`, section 5).
