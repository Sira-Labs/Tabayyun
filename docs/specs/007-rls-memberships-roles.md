# Spec 007 — Row-level security, memberships and roles

Sprint 8, story S8-3. Depends on: 001 (schema), 002 (runs and worker), 008 (datasets and
groups). Packages: `api/` (`db`, new `authz`, `services`, `jobs`, migration 0004),
`deploy/`. Decision record: ADR-0007; baseline items: `docs/frontend/02-security-baseline.md`,
"Authorization".

## Goal

Every tenant table is protected by Postgres row-level security keyed by `app.org_id`, and the
API and the worker connect as a role that neither owns the tables nor bypasses RLS, so a
query without the right tenant context sees and changes nothing. Users, org and workspace
memberships and teams exist as tables; one `authorize()` and one `visible_ids()` in the new
`tabayyun.authz` package decide what a principal may do. Every request and every job runs
inside a transaction that carries its org. Until spec 013 brings login, requests act as a
seeded bootstrap user who owns the default org, so today's behaviour is unchanged. Tests
with a second org prove that nothing crosses the tenant boundary.

## User story

As an org admin, I rely on the platform keeping one customer's series, runs and findings
invisible to every other org, even when application code has a bug, so that one install can
serve several tenants.

## Interface

### Database (migration 0004)

New tables. Roles are text with named CHECK constraints, as in spec 001.

| Table | Columns | Keys and RLS |
|---|---|---|
| `users` | `id uuid`, `email text` (stored lower-case), `display_name text`, `created_at`, `disabled_at null` | PK `id`, unique `email`. No `org_id`, no RLS: a user spans orgs. Nothing in this spec exposes users over HTTP (014 does, org-scoped). |
| `org_memberships` | `org_id`, `user_id`, `role` (`owner`, `admin`, `member`), `created_at` | PK (`org_id`, `user_id`); RLS by `org_id` |
| `workspace_memberships` | `org_id`, `workspace_id`, `user_id`, `role` (`admin`, `editor`, `viewer`), `created_at` | PK (`workspace_id`, `user_id`); RLS |
| `teams` | `id`, `org_id`, `name`, `created_at` | unique (`org_id`, `name`); RLS |
| `team_members` | `org_id`, `team_id`, `user_id`, `created_at` | PK (`team_id`, `user_id`); RLS |
| `workspace_team_roles` | `org_id`, `workspace_id`, `team_id`, `role` (`admin`, `editor`, `viewer`) | PK (`workspace_id`, `team_id`); RLS |

Changed tables: `uploads`, `metrics`, `scores`, `coverage`, `dataset_series` and
`series_group_members` gain `org_id uuid NOT NULL REFERENCES orgs`, backfilled from their
parent row. A policy that joins to a parent would run a subquery per row, which is slow on
the hypertables.

RLS on every tenant table (the tables above, `orgs`, `workspaces` and every table with
`TenantMixin`): `ENABLE ROW LEVEL SECURITY` (not `FORCE`, see Implementation edits), with
one policy per table:

```sql
CREATE POLICY tenant ON <table>
  USING (org_id = nullif(current_setting('app.org_id', true), '')::uuid)
  WITH CHECK (org_id = nullif(current_setting('app.org_id', true), '')::uuid);
-- orgs: the same with id in place of org_id
```

Without `app.org_id`, the setting reads as NULL: selects return no rows, and writes fail
with an RLS violation.

Roles: the migration creates `tabayyun_app` (NOLOGIN, NOSUPERUSER, NOBYPASSRLS). The role
gets DML on the schema's tables, USAGE on its sequences, the Procrastinate tables and
functions it needs, and default privileges so later migrations need no new grants. The login
the API and the worker use is a member of `tabayyun_app`, or the role itself (see Behaviour 1).

The stale-run reaper is the only job that works across orgs. It calls
`tabayyun_reap_stale_runs(older_than interval) RETURNS integer`, a `SECURITY DEFINER`
function owned by the migration owner, with a fixed `search_path`. The function can only
mark stale runs failed and delete their uploads.

Seed: the bootstrap user `00000000-0000-0000-0000-000000000003` (`bootstrap@tabayyun.invalid`),
owner of the default org. Spec 013 replaces it with real users; it is never a login.

### Settings

| Key | Default | Meaning |
|---|---|---|
| `TABAYYUN_DATABASE_URL` | dev URL | the app role's login; used by the api and the worker for all requests and jobs |
| `TABAYYUN_MIGRATION_DATABASE_URL` | unset → `TABAYYUN_DATABASE_URL` | the table owner, used only by `python -m tabayyun.db.migrate`; the worker never needs it |

### Python (`tabayyun.authz`)

```python
class OrgRole(StrEnum): OWNER, ADMIN, MEMBER
class WorkspaceRole(StrEnum): ADMIN, EDITOR, VIEWER
class Action(StrEnum): READ, WRITE, MANAGE      # viewer: read; editor: +write; admin: +manage

@dataclass(frozen=True)
class Principal: user_id: UUID; org_id: UUID
@dataclass(frozen=True)
class Scope: org_id: UUID; workspace_id: UUID       # what a service call reads and writes

async def workspace_role(session, principal, workspace_id) -> WorkspaceRole | None
async def authorize(session, principal, action, workspace_id) -> WorkspaceRole
    # raises NotVisibleError | ForbiddenError
def visible_workspaces(principal) -> Select[tuple[UUID]]               # the visible_ids() of ADR-0007
def require(action) -> Depends                   # ReadScope, WriteScope, ManageScope for routes
# tabayyun.db
def for_org(factory, org_id) -> async_sessionmaker   # sessions whose transactions set app.org_id
```

The effective workspace role is the highest of: the principal's direct workspace membership;
the roles of their teams in that workspace; and `admin` when their org role is `owner` or
`admin`. An org `member` without a workspace grant sees nothing in that workspace.
`NotVisibleError` maps to 404, and `ForbiddenError` (the workspace is visible, the action is
not allowed) maps to 403.

## Behaviour

1. `migrate upgrade head` connects with `TABAYYUN_MIGRATION_DATABASE_URL`. It creates the
   tables, backfills `org_id`, enables RLS and creates `tabayyun_app` with its grants. When
   the app URL names a different user from the owner, the migrate command makes sure that
   user exists as `LOGIN` in `tabayyun_app` with the URL's password, so operators only
   choose a password. Idempotent.
2. At startup, the api and the worker check their own login with `pg_roles`: not a
   superuser, no `BYPASSRLS`, and not the owner of the tenant tables. A failed check logs
   `db.rls_bypassed` with the login and the setting to change: as an error in `prod`, as a
   warning in `dev` and `test`. The process keeps serving, so a release merged before the
   operator has switched the login causes no outage. Spec 015 turns the prod error into
   exit code 4.
3. Every API request gets its session from `get_session`, which begins the transaction and
   runs `set_config('app.org_id', <principal.org_id>, true)`. The setting is
   transaction-local, so a pooled connection never carries it into the next request. The
   principal comes from a `get_principal` dependency; until 013 it returns the bootstrap
   user in the default org.
4. Every route authorizes its action in the request's workspace through `require()` and
   hands the service a `Scope`; services read and write only that scope's workspace. The
   workspace is the default one until the picker of spec 014, which lists workspaces with
   `visible_workspaces()`. Existing routes keep their shapes and status codes for the
   bootstrap principal.
5. Enqueueing a run passes `org_id` in the job arguments. `run_checks` executes with
   `for_org(factory, org_id)`, so every database step runs in that org. A job queued before this
   release has no `org_id` and runs in the default org, which held all data before 0004.
6. `reap_stale_runs` calls the `SECURITY DEFINER` function and returns its count.
7. Cross-tenant access: an id from another org is not found through any route (404),
   because the row is invisible, not because of an explicit check. A write that names
   another org's workspace fails `authorize()` with 404.
8. An RLS violation (Postgres `42501` from a policy) is logged as `db.rls_violation` with the
   method, the path and the error, and answers 500. It indicates a bug, not a user error.
   (There is no request id yet; the observability pass adds one.)
9. The schema guard treats a login that may not read `alembic_version` (a schema from
   before 0004, for example after a downgrade) as a mismatch: exit code 3.

## Acceptance criteria

- [x] Migration 0004 upgrades a database holding data from 0003 and downgrades cleanly,
      in both TimescaleDB modes; every row keeps its org after the backfill.
- [x] Connected as the app role without `app.org_id`, every tenant table returns zero
      rows and rejects inserts.
- [x] With org A's context, org B's rows are invisible and cannot be inserted, updated or
      deleted in every tenant table, hypertables included.
- [x] Every existing route answers 404 for an id from another org (parametrised test over
      the routes with path ids).
- [x] `authorize()` truth table: every combination of org role, direct workspace role and
      team role against `read`, `write` and `manage`.
- [x] A worker job for org B only touches org B's rows; a job without `org_id` runs in
      the default org.
- [x] The reaper still reaps stale runs across orgs, and the app role cannot run the
      update it performs directly.
- [x] Startup check: a superuser or owner login logs `db.rls_bypassed`, as an error in prod
      and a warning in dev; the app role logs nothing.
- [x] `deploy/README.md`, `deploy/caprover.md` and the compose files document the two URLs.
- [x] The live system runs with the app role: after a fresh database (25 Sep), the api
      starts as `tabayyun_app` without `db.rls_bypassed`; `/api/version` shows schema 0004
      and a connected worker on the release commit.
- [x] Baseline boxes ticked in `02-security-baseline.md`: single `authorize()` path, RLS on
      all tenant tables, API role not table owner, context per request and per job.

## Test cases

Unit (`api/tests`): `test_authz_roles.py`, the role-resolution truth table on in-memory
membership rows; `test_settings.py`, the migration URL fallback.

Integration (`api/tests/db`, need `TABAYYUN_TEST_DATABASE_URL`):
- `test_rls.py`: one seed fills every tenant table for two orgs. Policies per table: no
  context, other org, own org (listed from the metadata, so a new table without RLS fails);
  TimescaleDB chunks read directly; every route with a path id as the other org → 404 (the
  list of routes is checked against the OpenAPI paths); lists; viewer, team editor, member
  without a grant, disabled admin; `visible_workspaces`.
- `test_migrations.py::test_0004_backfills_org_ids_and_downgrades`: data at 0003 in a second
  org, upgrade, backfill, downgrade, both Timescale modes.
- `test_app_role.py`: the startup check against the owner and the app login, logs per
  environment, login provisioning (idempotent, the role itself as the login).
- `test_jobs.py`: a job for org B and a legacy job without an org; the reaper across two
  orgs while the app login alone sees nothing.
- `test_app.py`: an RLS violation is logged and answers 500.

The DB fixtures gain an app login (`tests/tenancy.py`). The app under test connects as that
login, never as the owner, so RLS is exercised rather than bypassed; the owner only seeds and
inspects.

## Implementation edits

- `ENABLE`, not `FORCE`, row-level security. `FORCE` would subject the owner too, which
  breaks the reaper's `SECURITY DEFINER` function whenever the owner is not a superuser; the
  app login is kept off the owner by the startup check instead.
- TimescaleDB chunks bypassed the policies when named directly (found while testing: 2 rows
  visible without context). Migration 0004 enables RLS without a policy on every chunk, and
  the `tabayyun_chunk_rls` event trigger does so for every new chunk; queries through the
  hypertable still apply its policy. Revoking the chunk schema instead also broke those
  queries.
- `users` is read-only to `tabayyun_app` (review finding): it has no RLS, so a write would
  reach users of every org. Spec 013 brings the controlled write path for logins.
- The login may be `tabayyun_app` itself (the deploy docs use that name); the migrate
  command then skips the self-grant.
- `authz.deps.get_session` replaces `db.get_session`: the request session needs the
  principal's org, so it depends on `get_principal`.
- Routes pass a `Scope` to services instead of calling `authorize()` inside each service;
  the worker builds the scope from the run row.

## Out of scope

- Login, sessions and real users: spec 013 (S8-2); the bootstrap principal is replaced there.
- Tenant APIs (orgs, workspaces, members, teams, invitations) and audit events: spec 014
  (S8-4).
- Resource shares and link shares: S12-1.
- Refusing to start in prod on a superuser or owner login (exit code 4): spec 015 (S8-6),
  once the live system runs with the app role.
- UUIDv7 identifiers (baseline item): a later security pass (015 or after).
- Workspace-level RLS: RLS is keyed by org only, as ADR-0007 decides; workspaces are
  separated by `authorize()` and `visible_workspaces()`.
