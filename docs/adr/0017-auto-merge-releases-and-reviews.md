# ADR-0017: Auto-merge to main, ship emails and release reviews

- **Status:** Accepted
- **Date:** 2026-10-02
- **Deciders:** owner

## Context

Every pull request waited for the owner to press "Merge", although the owner reviews outcomes
on staging, not diffs. `main` deploys to staging on every merge, and production only runs
what the owner approves in `promote.yml` (ADR-0016). So the merge click added waiting time,
not safety. The owner wants to learn what shipped by email, and to see bigger releases in a
presentation with a calendar slot, instead of following pull requests.

## Decision

- **Auto-merge.** Pull requests merge themselves once the `protect-main` ruleset is met:
  - the four CI jobs are green;
  - CodeRabbit's review is complete (its `CodeRabbit` commit status is required, so a merge
    never lands before the review);
  - every review thread is resolved.

  Claude Code enables GitHub's auto-merge (merge commit) on each pull request it opens, as
  the owner's account, so the merge's push to `main` starts `release.yml`. GitHub does not
  start workflows for merges made with a workflow's `GITHUB_TOKEN`, which rules out a bot
  that turns auto-merge on. Pull requests that need an owner step before they may deploy (a
  setting, a secret, a Keycloak change) stay drafts until the owner has done it.
- **Production stays gated.** Promotion to production still needs the owner's approval of a
  `promote.yml` run (ADR-0016).
- **Ship email.** `deploy to staging` and `deploy to production` end with a mail to
  `SHIP_MAIL_TO`. It lists the pull requests between the commit that was live before and the
  one now live, with links, or says that the deploy failed. It is sent over SMTP with a
  Google Workspace app password (`SMTP_USERNAME`, `SMTP_PASSWORD`). Without them the step
  only logs a notice, and a mail error never fails a deploy.
- **Release reviews.** When the last story of a sprint is on staging, or a production
  promotion carries new features, Claude Code prepares a slide deck of what shipped and
  books a review in the owner's Google calendar (`CLAUDE.md`, "Shipping").

## Alternatives considered

| Option | Pros | Cons | Why not |
|---|---|---|---|
| Owner merges every PR | a human look before staging | waiting time; the owner judges results on staging anyway | the owner asked for auto-merge |
| A workflow that enables auto-merge | works without a Claude session | merges made with `GITHUB_TOKEN` start no workflows, so nothing would deploy; a PAT or app token would be another secret | the PR's author enables it |
| GitHub release notifications instead of mail | no SMTP secret | one release per merge clutters releases; delivery depends on watch settings | mail says exactly what shipped |
| A mail action from the marketplace | less code | a third-party action sees the SMTP password | a short standard-library script |

## Consequences

- A change reaches staging without a human look at the diff. The gates are CI, CodeRabbit,
  resolved threads and Claude Code's own validation before pushing. Production keeps its
  human approval.
- A CodeRabbit outage keeps pull requests open, because its status never arrives. The owner
  can merge by hand in that case.
- Owner set-up, once: allow auto-merge, update the `protect-main` ruleset, and add the SMTP
  secrets and `SHIP_MAIL_TO` (`deploy/caprover.md`, section 5).
