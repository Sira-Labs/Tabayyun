# Security policy

Tabayyun handles operational data from industrial sites, so we treat security reports
seriously and aim for the ASVS L2 baseline in `docs/frontend/02-security-baseline.md`.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private reporting:
<https://github.com/Sira-Labs/Tabayyun/security/advisories/new>.

Include the affected component (core, bindings, api, web, deploy), a version or commit, steps
to reproduce, and impact. You should hear back within 5 working days. We will keep you
informed while we triage, fix and publish an advisory, and credit you unless you prefer not.

## Supported versions

Tabayyun is pre-1.0. Only the `main` branch and the most recent `v*` tag receive fixes.

## Scope

In scope: the code in this repository, the published container images
(`ghcr.io/sira-labs/tabayyun-api`, `ghcr.io/sira-labs/tabayyun-web`) and the compose
bundles in `deploy/`. Out of scope: third-party services you integrate (Keycloak, PI Web API,
OPC UA servers) except where Tabayyun's use of them is at fault; findings that require a
compromised host or administrator credentials.

## Design notes for reporters

- No secrets in the repository; configuration comes from environment variables and production
  refuses placeholder values.
- Authorization is enforced in Postgres with row-level security (ADR-0007); a bypass of RLS is
  always in scope.
- Corrections never overwrite raw data (ADR-0010); anything that mutates a raw layer is in scope.
- Images are built with SBOM and provenance attestations; `cargo audit`, `pip-audit` and
  `pnpm audit` run in CI.
