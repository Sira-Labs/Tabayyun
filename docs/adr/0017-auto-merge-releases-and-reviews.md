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
- for it, slides and a one-hour calendar slot;
- the same way as Suffa and Thawr: a release for every change that reaches staging, and the
  mail, slides and calendar slot from a release notifier routine.

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
- **A release for every change on staging** (Suffa's scheme, `docs/ops/releases.md` there).
  Right after `deploy to staging` sees the new commit live, `release.yml` calls
  `cut-release.yml`, which publishes the next version of that commit:
  - **patch:** every merge that reaches staging;
  - **minor:** a merge whose commit message has a line starting with `[minor]`, used when the
    last story of a sprint lands. The pull request's title starts with it, and GitHub's merge
    commit carries the title;
  - **major:** a line starting with `[major]`, only when the owner names a milestone.

  Without any tag the first release follows the code's version, `0.1.0`
  (`api/pyproject.toml`). A commit that already has a version is not released again. Run by
  hand on `main`, the workflow lets one choose the part, or cut a candidate (`vX.Y.Z-rc.N`),
  after checking that staging runs the head. The `v*` tag is pushed with the owner's token:
  only organisation admins may create those tags, and only such a push starts `release.yml`
  for the versioned images. A tag run builds those images and does not deploy staging again.
  The release notes list the merged pull requests since the previous version. Before 1.0 a
  minor version may break APIs (`CHANGELOG.md`).
- **Release notifier.** The "Tabayyun release notifier" routine in the owner's Claude account
  runs at 08:47 and 20:47 Riyadh time, as Thawr's does. For each new release it emails the
  owner "[Tabayyun] <tag> shipped": what changed, what to check on staging, and links. For a
  stable minor or major release it first makes a slide deck and books a one-hour
  "Tabayyun <tag> release presentation" in the owner's Google calendar, with the deck linked.
  It uses the owner's Gmail and Calendar connections, so the repository holds no mail
  secret. Merges and deploys send no mail: a failed workflow already triggers GitHub's own
  notification to the owner, as whose account merges and releases run.
- **Production stays gated.** Promotion still needs the owner's approval of a `promote.yml`
  run (ADR-0016).

## Alternatives considered

| Option | Pros | Cons | Why not |
|---|---|---|---|
| Owner merges every PR | a human look before staging | waiting time; the owner judges results on staging anyway | the owner chose auto-merge |
| Enable auto-merge with `GITHUB_TOKEN` | no extra secret | its merges start no workflows, so nothing would deploy | the owner's token |
| Auto-merge for forks too | nothing waits | untrusted code reaches staging without a person | forks wait for a person |
| Releases cut by hand only | fewer, larger releases | nothing reaches the owner until someone remembers to cut one | a release per change, as in Suffa |
| release-please | changelog and version bumps from commits | its PRs, opened with `GITHUB_TOKEN`, start no CI, so they can never merge | the commit message picks the bump |
| The release mail sent by CI over SMTP | works without a Claude session | an SMTP app password in GitHub; only a list of PR titles; no slides or calendar slot | the notifier routine does all three, as for Thawr |

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
- The mail depends on the notifier routine, not on the repository. If the routine is paused,
  releases still publish and can be read on GitHub.
- Owner set-up, once: allow auto-merge, activate the rulesets, and add `AUTOMATION_TOKEN`
  (`deploy/caprover.md`, section 5).
