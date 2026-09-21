# Authorization and sharing design

## Principals and roles

```
Organisation
 ├─ org roles:        owner, admin, member, billing (org-wide)
 └─ Workspace
     ├─ workspace roles: admin, editor, viewer
     ├─ Team (workspace-scoped group with one workspace role)
     └─ Resource (dataset, suite, dashboard, report, finding view)
         └─ share: (user | team | link) × (editor | viewer) × expiry
```

Effective role on a resource = **max** of: org role mapping (owner/admin → admin),
workspace membership role, team role, direct resource share. Deny by default.

| Action | viewer | editor | admin | org owner |
|---|---|---|---|---|
| view findings, scores, series | ✔ | ✔ | ✔ | ✔ |
| ack / mute / resolve findings | | ✔ | ✔ | ✔ |
| edit datasets, suites, thresholds | | ✔ | ✔ | ✔ |
| manage sources and credentials | | | ✔ | ✔ |
| manage members, teams, shares | | | ✔ | ✔ |
| org settings, SSO, billing, delete workspace | | | | ✔ |
| read audit log | | | ✔ (workspace) | ✔ (org) |

## Enforcement

1. **Single choke point.** `authz.authorize(principal, action, resource)` in the API; route
   handlers never inline permission logic. `authz.visible_ids()` produces the filter for
   list endpoints.
2. **Postgres RLS as a safety net.** Every tenant table has `org_id`; policies compare
   against `current_setting('app.org_id')`, set per request/job with `SET LOCAL`. The API
   role is *not* table owner. Background jobs set the context explicitly.
3. **Cross-tenant tests in CI.** For every list/get endpoint: user in org A requests a
   resource of org B → 404 (not 403, to avoid existence leaks).
4. **IDs are UUIDv7.** No sequential IDs in URLs.
5. **Audit.** Every authz-relevant mutation (membership, role, share, credential) writes an
   `audit_events` row.

## Sharing

### Share with a person or team
A `shares` row `(resource_type, resource_id, subject_type=user|team, subject_id, role,
expires_at, created_by)`. Shared users see the resource in a "Shared with me" list; they
do not gain workspace membership.

### Share by link
- Token: 32 random bytes from the OS CSPRNG, base64url in the URL; **only the SHA-256 hash is
  stored**.
- Row: `(token_hash, resource_type, resource_id, role=viewer, scope JSON, expires_at,
  password_hash?, created_by, revoked_at, view_count)`.
- Scope is *locked*: fixed series set, fixed time range or relative window, fixed filters.
  A link viewer cannot widen the query.
- Access path: `/share/:token` → API resolves the hash, checks expiry/revocation/password,
  then calls `authorize()` with a synthetic `LinkViewer` principal. Everything downstream is
  the same code path as a logged-in viewer.
- Rate limited per IP and per token; every view is audit-logged; org admins can disable
  public links workspace-wide and see/revoke all links.

### Embeds (later)
Signed JWT (HS256 with per-org secret, `exp` ≤ 1 h) with locked parameters, served in an
iframe with `frame-ancestors` restricted to the customer's domains.

## Upgrade path
If sharing inheritance becomes a graph (folders of dashboards, teams of teams), move the
relationship resolution to OpenFGA behind the same `authorize()` interface. Nothing in the
route handlers changes.
