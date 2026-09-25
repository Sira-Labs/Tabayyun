# ADR-0016: Separate production server; the current host becomes staging and tools

- **Status:** Accepted
- **Date:** 2026-09-25
- **Deciders:** owner (Sīra family decision, Arqam ADR-0020)

## Context

Tabayyun, Arqam and Suffa run on one Hetzner server with one CapRover. Tabayyun's release
workflow deployed every push to `main` straight to the apps at `tabayyun.siralabs.org`. So
the only environment was production, and it ran whatever `main` was. Today only the owner,
family and friends use the apps, which makes this the cheapest moment to separate them.

One host for everything means one failure (a runaway worker, a full disk, a staging
experiment) takes every project's production down. It also puts real people's data next to
test data, and gives backups no line between "must never be lost" and "can be rebuilt". The
family decided the split once for all three projects (Arqam ADR-0020, 25 Sep 2026); this
ADR adopts it for Tabayyun.

## Decision

- **Two independent CapRover servers at Hetzner.**
  - The **current server becomes staging and tools**: `tabayyun-*-stg` apps at
    `tabayyun-stg.siralabs.org`, GlitchTip with the uptime checks for both servers, and
    experiments.
  - A **new server in Germany becomes production**: `tabayyun-*` apps at
    `tabayyun.siralabs.org` with their own RustFS (the Parquet cache), and Keycloak once
    it serves real users.
- **Data:** real people's data lives only on production. Staging holds test data. People who
  used the current install sign up again on production; nothing is copied across.
- **Promotion, not rebuild:**
  - `release.yml` builds and scans the images once per commit and deploys `main` (and `v*`
    tags on `main`) to staging.
  - `promote.yml` deploys the **same digests** to production after two checks: the commit
    is on `main`, and staging serves it on web, api and worker.
  - A promotion runs only after the owner's approval: the `production` environment's
    required reviewer.
- **Release guard:** deploy jobs bind an environment. `staging` allows `main` and `v*` tags;
  `production` allows `main` and has the owner as required reviewer. The CapRover tokens,
  `DEPLOY_SSH_KEY` and server variables live on those environments, not at repository
  level. A tag ruleset lets only organisation admins create `v*` tags. The rules hold even
  when a branch edits a workflow file, because GitHub enforces them before a job sees an
  environment secret.
- **Separate secrets:** production has its own database passwords, session secret, S3 keys,
  OAuth clients and CapRover app tokens. The production server allows SSH by key only, and
  its firewall opens only 80, 443 and 22. The CapRover dashboard uses a strong password and
  2FA.
- **Backups of production Postgres:**
  - continuous WAL archiving (WAL-G or pgBackRest) on physical base backups: a weekly full,
    a daily delta, at least two fulls kept;
  - the nightly `pg_dump -Fc`;
  - a versioned copy of the Parquet cache bucket, which holds the only copy of uploaded
    series;
  - all encrypted, to object storage in another Hetzner location.
- **Restore drills** restore into a throwaway database on the production server, are timed,
  and are dropped afterwards. They never restore into staging.

## Alternatives considered

| Option | Pros | Cons | Why not |
|---|---|---|---|
| One server, separate by app name | Free | Shared failures and mixed data stay | The problems above remain |
| One CapRover cluster with two nodes | One dashboard | Shared control plane; a misconfiguration reaches both | More moving parts for a solo operator |
| Keep deploying `main` to production, approve in the PR | No second server | No tested artifact before production; no place for experiments | Promotion of a tested digest is the point |
| Managed Postgres | Less to operate | Cost; TimescaleDB availability; another subprocessor | Revisit at scale |

## Consequences

- One more server to pay for and patch (unattended security updates on both).
- The repository-level `CAPROVER_*` values move into the two environments. Until the
  `staging` environment has `CAPROVER_SERVER`, `deploy-staging` is a no-op.
- The live install at `tabayyun.siralabs.org` becomes production when the new server
  serves. Until then it is the only install, and nothing deploys to it automatically.
- `deploy/caprover.md` describes both servers, the GitHub settings and the backup
  requirements (sections "Staging and production", 5 and 7).
- Follow-up work, tracked in TASKS.md:
  - build the backups and run the first timed restore drill;
  - production Keycloak (with spec 013 or before the first real user);
  - error reporting to GlitchTip.
- Owner tasks: order the server, sign Hetzner's DPA, choose the backup location, set up the
  environments and the tag ruleset, and move the domain.
