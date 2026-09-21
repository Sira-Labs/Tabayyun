# ADR-0012: Licence the project under Apache-2.0

- **Status:** Proposed
- **Date:** 2026-09-21
- **Deciders:** Markus Clauss

## Context

The README deferred the licence decision until R1 code landed. R1 code has landed and the
repository is public, so every clone is currently "all rights reserved" by default: nobody
can legally run, modify or contribute to it, and package metadata says `UNLICENSED`. The
roadmap mentions a public benchmark corpus (R2), a plugin ecosystem (ADR-0009), pilot
installations at customer sites, and an "offline licensing" option (R3), so the licence has
to allow self-hosting by third parties while leaving room for a commercial offering later.

## Decision

Release the whole repository (Rust core, Python API, web client, deployment bundles, docs)
under the **Apache License 2.0**, with a `NOTICE` file carrying the copyright line. Package
metadata (`core/Cargo.toml`, `api/pyproject.toml`, `core/tabayyun-py/pyproject.toml`,
`web/package.json`) declares `Apache-2.0`. Contributions are accepted under the same
licence via the inbound=outbound rule in `CONTRIBUTING.md`; no CLA.

Third-party datasets used for the R2 benchmark corpus keep their own licences (CC-BY) and are
published separately, not vendored into this repository.

## Alternatives considered

| Option | Pros | Cons | Why not |
|---|---|---|---|
| Apache-2.0 | Permissive, explicit patent grant, standard for Rust/Arrow/Python data infrastructure (Arrow, Polars, Great Expectations), no friction for pilots or plugin authors | Competitors may host it as a service | Chosen: adoption and contributions matter more than SaaS protection at this stage |
| MIT | Simplest permissive licence | No patent grant; weaker for an enterprise audience | Apache-2.0 gives the same freedom plus patent clarity |
| AGPL-3.0 | Protects against closed SaaS forks; common for self-hosted commercial OSS (Grafana, Nextcloud) | Many energy utilities and integrators forbid AGPL code; blocks embedding the core wheel in customer pipelines; deters plugin authors | Conflicts with the "local by default, embed the core" positioning |
| BSL 1.1 / SSPL | Strongest commercial protection | Not open source (OSI); pilot customers' legal teams push back; incompatible with an ecosystem of sandboxed plugins | Can still be adopted for a future "enterprise" add-on module without relicensing the core |

## Consequences

- Anyone can self-host, fork and embed Tabayyun; commercial differentiation must come from
  hosted operation, support, enterprise add-ons (R3: SSO, SCIM, SOC 2 pack), not from the
  core licence.
- Changing to a copyleft or source-available licence later requires agreement from every
  contributor; do it, if ever, before external contributions accumulate (or introduce a CLA
  then, not now).
- All new source files may carry the short Apache header; existing files are covered by the
  root `LICENSE` and this ADR without per-file headers.
- Dependency licences must stay Apache-compatible: `cargo deny` / `pip-licenses` checks are
  follow-up work for the v0.1 milestone.
