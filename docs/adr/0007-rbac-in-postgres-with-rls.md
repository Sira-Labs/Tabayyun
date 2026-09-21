# ADR-0007: RBAC in Postgres tables with a single authorize() and RLS safety net

- **Status:** Accepted
- **Date:** 2026-09-21

## Context
Org → workspace → resource hierarchy, roles owner/admin/editor/viewer, teams, and
resource-level sharing to users, teams and links.

## Decision
Membership and share tables in Postgres; one `authorize()` and one `visible_ids()` in the
API compute effective roles; row-level security on all tenant tables keyed by
`app.org_id` set per request and per job. Cross-tenant tests in CI. Move to OpenFGA or Cerbos
behind the same interface only when rules outgrow SQL.

## Alternatives considered
| Option | Pros | Cons | Why not |
|---|---|---|---|
| OpenFGA / SpiceDB now | Graph inheritance, proven model | Another service, eventual consistency, ops | Not needed for a 3-level hierarchy |
| Casbin | Embedded | Weak tooling, easy to misconfigure | Less transparent than SQL |
| Oso library | Nice DSL | Deprecated | Dead end |

## Consequences
Policies live in reviewable SQL and Python. RLS pitfalls (table owner bypass, pooler
session state) are documented in the ops runbook.
