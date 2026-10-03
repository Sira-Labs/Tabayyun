# Spec 014 — Tenant APIs, invitations, audit log and admin panel v1

Sprint 8, stories S8-4 and S8-5. Depends on: 007 (memberships, teams, `authorize()`, RLS),
013 (sessions, `tabayyun_login`, `require_recent_passkey()`). Packages: `api/` (migration
0006, `authz`, new `services/admin`, new `mail`, `jobs`, routers), `web/` (workspace picker,
admin panel), `deploy/` (SMTP settings). Decisions confirmed by the owner on 3 Oct 2026 (see
"Decisions").

## Goal

An org admin runs the organisation from the browser. Every admin action needs a passkey
sign-in from the last 12 hours (spec 013's gate). Admins can:

- invite people by email with a role;
- change roles and remove members;
- create workspaces and decide who works in each, directly or through teams;
- read an append-only audit log of every change.

An invited person signs in with Google, GitHub or a passkey under the invited address and is a
member right away. A person with access to several workspaces switches between them in the
header, and every data route acts in the chosen one. One install still holds one organisation.

## User story

As an org admin, I invite a colleague by email as an editor of the "Plant North" workspace.
They sign in with Google and see that workspace's runs, and nothing of "Plant South". Later I
can read who granted that access and when.

## Interface

### Database (migration 0006)

| Table | Columns | Keys, RLS and grants for `tabayyun_app` |
|---|---|---|
| `invitations` | `id` uuid, `org_id`, `email` text (lower-case), `org_role` (`admin`, `member`), `workspace_id` null → workspaces (cascade), `workspace_role` null (`admin`, `editor`, `viewer`), `invited_by` → users, `created_at`, `expires_at`, `accepted_at` null, `accepted_user_id` null, `revoked_at` null, `email_status` (`not_configured`, `queued`, `sent`, `failed`), `email_error` text null | PK `id`. Partial unique (`org_id`, `email`) where `accepted_at` and `revoked_at` are null. CHECK: `workspace_id` and `workspace_role` are both set or both null. RLS by `org_id`; SELECT, INSERT, UPDATE |
| `audit_events` | `id` uuid, `org_id`, `workspace_id` null (no FK, so events outlive a deleted workspace), `actor_user_id` null (null = the system), `action` text, `target_type` text, `target_id` text, `details` jsonb, `ip_address` inet null, `user_agent` text null, `created_at` | PK `id`. Indexes (`org_id`, `created_at` desc, `id` desc) and (`org_id`, `workspace_id`, `created_at` desc). RLS by `org_id`; **SELECT and INSERT only**, so the app cannot change or delete an event |

New SECURITY DEFINER functions (owner-run, fixed `search_path`, EXECUTE granted to
`tabayyun_app` only):

- `tabayyun_accept_invitations(p_user uuid) RETURNS SETOF uuid` accepts every pending, unexpired
  invitation whose email equals the user's verified email. For each one it:
  - adds the org membership, and keeps an existing higher role;
  - adds the workspace grant when there is one, and keeps an existing higher role;
  - marks the invitation accepted;
  - writes an audit event `member.joined`.
  It returns the orgs joined, one per accepted invitation. A disabled user accepts nothing.
- `tabayyun_login(...)`: same signature and result as spec 013. It now calls
  `tabayyun_accept_invitations` after it resolves the user and before it picks the org.

### Settings

| Name | Default | Meaning |
|---|---|---|
| `TABAYYUN_SMTP_HOST` | empty | Empty: email is off. Invitations still work, and their `email_status` is `not_configured` |
| `TABAYYUN_SMTP_PORT` | `587` | |
| `TABAYYUN_SMTP_STARTTLS` | `true` | STARTTLS is required when true |
| `TABAYYUN_SMTP_USERNAME`, `TABAYYUN_SMTP_PASSWORD` | empty | Optional: the Google Workspace relay admits the server by IP |
| `TABAYYUN_SMTP_FROM` | empty | Sender, e.g. `Tabayyun <tabayyun@data-and-ai-dude.com>`; required when the host is set |
| `TABAYYUN_INVITATION_TTL` | `14d` | How long an invitation stays open |

In `prod`, a set host with an empty or placeholder `SMTP_FROM`, or a placeholder password,
refuses to start. The invitation email links to `TABAYYUN_PUBLIC_URL` + `/login`.

### Workspace context (every data route)

- `GET /api/workspaces` (any member) returns `[{"id", "name", "timezone", "created_at", "role"}]`. It lists
  the workspaces from `visible_workspaces()`, sorted by name, with the effective role in each.
- `X-Tabayyun-Workspace: <uuid>` selects the workspace for the existing data routes (series,
  runs, findings, groups, datasets, sources). `get_workspace_id` reads the header.
  - Without the header, the request acts in the default workspace when it is visible, else
    in the oldest visible one. Scripts such as `seed.py` therefore keep working.
  - A malformed id gives 400 `invalid_workspace`. A workspace the principal cannot see gives
    404, as in spec 007.

### Admin routes (`/api/admin`)

Every route needs a session with a fresh passkey (`require_recent_passkey()`, 403
`second-factor-required`) and the role named in the table. "Org admin" means org role `owner`
or `admin`. "Ws admin" means effective workspace role `admin`, which org admins hold in every
workspace. A role that is too low gives 403 `forbidden`. Ids of another org, or of
workspaces the caller cannot see, give 404.

| Route | Who | Request → response |
|---|---|---|
| `GET /api/admin/org` | org admin | `{"id", "name", "created_at", "role"}` |
| `PATCH /api/admin/org` | owner | `{"name"}` → the org |
| `GET /api/admin/members?q=&cursor=&limit=50` | org admin; ws admin of any (read) | `{"items": [{"user_id", "email", "display_name", "role", "joined_at", "disabled"}], "next": cursor or null}`. Sorted by email; `q` matches email or name (case-insensitive substring); `limit` ≤ 200 |
| `PATCH /api/admin/members/{user_id}` | org admin | `{"role": "owner" \| "admin" \| "member"}` → the member |
| `DELETE /api/admin/members/{user_id}` | org admin | 204 |
| `GET /api/admin/invitations?status=pending` | org admin | `[Invitation]`, newest first. `status` ∈ `pending`, `all` |
| `POST /api/admin/invitations` | org admin | `{"email", "org_role", "workspace_id"?, "workspace_role"?}` → 201 `Invitation` |
| `POST /api/admin/invitations/{id}/resend` | org admin | 200 `Invitation`: a new expiry, and the email is queued again |
| `DELETE /api/admin/invitations/{id}` | org admin | 204 (revoked) |
| `GET /api/admin/teams` | org admin; ws admin of any (read) | `[{"id", "name", "members": [{"user_id", "email", "display_name"}], "workspaces": [{"workspace_id", "name", "role"}]}]` |
| `POST /api/admin/teams` | org admin | `{"name"}` → 201 team |
| `PATCH /api/admin/teams/{id}` | org admin | `{"name"}` → team |
| `DELETE /api/admin/teams/{id}` | org admin | 204; its workspace roles go with it |
| `PUT /api/admin/teams/{id}/members/{user_id}` | org admin | 204; the user must be an org member (404 otherwise) |
| `DELETE /api/admin/teams/{id}/members/{user_id}` | org admin | 204 |
| `GET /api/admin/workspaces` | ws admin of any | `[{"id", "name", "timezone", "created_at", "role"}]`, the workspaces the caller administers |
| `POST /api/admin/workspaces` | org admin | `{"name", "timezone"}` → 201 workspace |
| `PATCH /api/admin/workspaces/{id}` | ws admin | `{"name"?, "timezone"?}` → workspace |
| `DELETE /api/admin/workspaces/{id}` | owner | 204 |
| `GET /api/admin/workspaces/{id}/access` | ws admin | `{"members": [{"user_id", "email", "display_name", "role"}], "teams": [{"team_id", "name", "role"}], "org_admins": [{"user_id", "email", "display_name", "role"}]}` |
| `PUT /api/admin/workspaces/{id}/members/{user_id}` | ws admin | `{"role"}` → 204; the user must be an org member |
| `DELETE /api/admin/workspaces/{id}/members/{user_id}` | ws admin | 204 |
| `PUT /api/admin/workspaces/{id}/teams/{team_id}` | ws admin | `{"role"}` → 204 |
| `DELETE /api/admin/workspaces/{id}/teams/{team_id}` | ws admin | 204 |
| `GET /api/admin/audit?workspace_id=&action=&actor=&before=&limit=50` | org admin; ws admin with `workspace_id` | `{"items": [AuditEvent], "next": cursor or null}`, newest first. `before` is the cursor. `action` is a prefix, e.g. `member.` |

- `Invitation` is `{"id", "email", "org_role", "workspace": {"id", "name"} | null,
  "workspace_role", "invited_by": {"user_id", "display_name"}, "created_at", "expires_at",
  "status": "pending" | "accepted" | "revoked" | "expired", "email_status", "email_error"}`.
- `AuditEvent` is `{"id", "created_at", "actor": {"user_id", "email", "display_name"} | null,
  "action", "target_type", "target_id", "workspace_id", "details", "ip_address"}`.

**Audit actions:**

| Area | Actions |
|---|---|
| Org | `org.renamed` |
| Workspaces | `workspace.created`, `workspace.updated`, `workspace.deleted` |
| Members | `member.joined`, `member.role_changed`, `member.removed` |
| Invitations | `invitation.created`, `invitation.resent`, `invitation.revoked` |
| Teams | `team.created`, `team.renamed`, `team.deleted`, `team.member_added`, `team.member_removed` |
| Workspace access | `workspace.member_set`, `workspace.member_removed`, `workspace.team_set`, `workspace.team_removed` |

`details` holds the changed values as `{"before": …, "after": …}` and the names involved
(email, workspace and team names), so the log stays readable after a deletion.

### Jobs

`send_invitation_email(invitation_id, org_id)` runs on a new `mail` queue with 3 retries and
exponential backoff. The email is plain text. It names the org, the inviter and the role, and
gives the sign-in link and the expiry date. After the last attempt the invitation's
`email_status` is `failed` with the SMTP error, never the credentials.

### Web

- **Workspace picker.**
  - The header shows the current workspace. With more than one, it is a menu that switches.
  - The choice is kept in `localStorage` (`tby.workspace`) and sent as `X-Tabayyun-Workspace`
    on every API call. Switching clears the query cache and goes to `/runs`.
  - A stored id that is no longer visible falls back to the first workspace in the list.
  - A member with no workspace sees "No workspace yet" with their admin's hint text.
- **Admin entry.** `Admin` appears in the header for org admins and workspace admins and opens
  `/admin`, which has these tabs:
  - **Members:** list with search and role menus; remove with confirmation. Invitations
    (pending first) with status, email status, resend and revoke. An "Invite" form takes
    email, org role and an optional workspace and role.
  - **Workspaces:** create; rename and change timezone; delete with confirmation (owners).
    Per workspace: an access list for members and teams with role menus, add and remove,
    and the org admins shown read-only.
  - **Teams:** create, rename, delete; add and remove members; the team's workspace roles.
  - **Audit log:** newest first, filters for action and workspace, "Load more".
  - Workspace admins who are not org admins see only the Workspaces tab, limited to their
    workspaces, and the audit log of those.
- **Passkey gate.** When an admin call answers `second-factor-required`, the panel replaces its
  content with an explanation and a "Sign in with a passkey" button
  (`/api/auth/login?method=passkey&next=/admin`). A link to "Manage passkeys" helps an admin
  who has none yet.

## Behaviour

1. **Workspace resolution.** It follows "Workspace context" above. The resolved id goes
   through `require()` exactly as in spec 007. The response of a data route does not change.
2. **Admin gate.** The order is: session (401), passkey freshness (403
   `second-factor-required`), then role (403 `forbidden` or 404). In `dev` mode the passkey
   gate passes (spec 013), and the bootstrap principal is an owner.
3. **Members.**
   - Only an owner may grant `owner`, change an owner's role or remove an owner. An admin who
     tries gives 403 `forbidden`.
   - The last owner cannot be demoted or removed: 409 `last_owner`. The bootstrap user does
     not count as an owner, as in spec 013.
   - Removing a member deletes their workspace grants and team memberships in the org. It
     revokes their sessions in the org, so their next request gives 401. A user can remove
     themselves; the last owner cannot.
   - An unknown user, or one outside the org, gives 404.
4. **Invitations.**
   - The email is trimmed, lower-cased and checked for a plausible address: 422
     `invalid_email`.
   - The roles follow the table above: `owner` cannot be invited (422 `invalid_role`). A
     workspace role without a workspace, or the reverse, gives 422.
   - An email that already belongs to a member gives 409 `already_member`. One with a pending
     invitation gives 409 `invitation_pending`, so the admin resends instead.
   - An expired invitation does not block a new one: creating it marks the old one revoked.
   - Creating or resending queues the email when SMTP is set (`email_status` `queued`), and
     records `not_configured` otherwise. The invitation is valid either way.
   - Revoking a pending invitation gives 204. Revoking one that is accepted, revoked or
     expired gives 409 `not_pending`.
5. **Joining.**
   - `tabayyun_login` accepts matching invitations on every sign-in. The email is the
     verified one from the ID token; spec 013 already refuses unverified emails.
   - A session that signed in before the invitation existed has no org. Its next
     `GET /api/auth/me` runs `tabayyun_accept_invitations`. When the user now has a
     membership, the session's org is set and `/me` answers 200, so the "No access yet" page
     only needs a reload (its new "Check again" button).
   - An invitation for an email that never signs in simply expires.
6. **Teams.**
   - Team names are unique per org: 409 `name_taken`. A name is 1–100 characters after
     trimming: 422.
   - Adding someone who is not an org member gives 404. Adding an existing member is a no-op
     (204, no audit event).
7. **Workspaces.**
   - Names are unique per org: 409 `name_taken`. The timezone must be an IANA zone known to
     `zoneinfo`: 422 `invalid_timezone`.
   - Delete gives 409 `default_workspace` for the default workspace. It gives 409
     `workspace_not_empty` while the workspace has any source, series, dataset, group or
     run. Deleting the data first is a later story.
   - Workspace grants accept only org members (404 otherwise). Setting a grant replaces the
     previous role.
8. **Audit.**
   - Every mutation above writes one `audit_events` row in the same transaction, so a failed
     change leaves no event and an event never lacks its change. Each row records the actor,
     the IP (from `X-Real-IP`, as in spec 013) and the user agent.
   - A no-op, such as setting the role a member already has, writes nothing.
   - Reads are not audited.
   - The audit route is keyset-paginated by (`created_at`, `id`).
9. **Email.**
   - The job reads the invitation in its org (`for_org`). It sends only while the invitation
     is still pending; a revoked one is skipped.
   - It sets `email_status` to `sent` or, after the last retry, `failed`. Logs carry
     `mail.sent` and `mail.failed` with the invitation id and the SMTP reply code, never the
     password.
10. **RLS.** Every new table is a tenant table under spec 007's policy test. The cross-tenant
    test covers the new routes with path ids: another org's id gives 404.

## Acceptance criteria

- [x] Migration 0006 upgrades and downgrades cleanly. `audit_events` refuses UPDATE and
      DELETE for the app login. The new tables pass the RLS policy test.
- [x] `X-Tabayyun-Workspace` selects the workspace for data routes, with the fallback and the
      400 and 404 paths; `GET /api/workspaces` lists visible workspaces with roles.
- [x] Every admin route enforces session, passkey freshness and role in that order. A
      parametrised test runs each route with no session, a stale passkey, a Google session, a
      member and a workspace admin.
- [x] Owner rules and the last-owner guard hold for role changes and removals. Removing a
      member revokes their sessions in the org.
- [x] An invitation is created, emailed (fake SMTP), resent and revoked with the stated status
      codes. A sign-in with the invited verified email joins with the invited org and
      workspace roles. A signed-in user without access joins on the next `/me`.
- [x] Teams and workspace grants change the effective role exactly as `authorize()` computes it
      (the invitee in the user story sees Plant North and gets 404 for Plant South).
- [x] Every mutation writes exactly one audit event with actor, IP and details. A failed
      mutation writes none. The audit route filters and paginates.
- [x] `prod` refuses an SMTP host without a sender, or with a placeholder password.
- [x] The web app switches workspaces and every page reads the chosen one. All admin actions
      are possible from `/admin`. The passkey gate shows its explanation and button.
- [ ] Staging: the owner invites a second Google account as viewer of a new workspace. The
      email arrives; the invitee signs in and sees only that workspace. The audit log shows
      both steps.
- [x] `02-security-baseline.md` records the passkey gate on every admin route and the audit
      log of authz changes. Both items stay open, because they also cover per-organisation MFA
      and auth events, shares and the SIEM export (spec edited: the baseline has no separate
      "passkeys for admins" item).

## Test cases

**Unit (`api/tests`):**
- `test_admin_rules.py`: the owner rules and last-owner guard as a pure function over member
  lists.
- `test_invitation_validation.py`: email normalisation, role combinations, expiry and status.
- `test_mail.py`: message text and headers, and an SMTP refusal mapped to `failed`. It uses an
  in-process fake SMTP server (aiosmtpd), never a real host.
- `test_settings.py`: SMTP prod refusals.

**Integration (`api/tests/db`):**
- `test_workspace_context.py`: header, fallback, 400 and 404.
- `test_admin_members.py`: list, search and paging; role changes; removal and session
  revocation; last owner.
- `test_admin_invitations.py`: create, conflict, resend, revoke and expiry; the email job
  against fake SMTP; join at login through the fake IdP of spec 013; join on `/me`.
- `test_admin_teams_workspaces.py`: teams, members, workspace CRUD, grants, delete guards, and
  the user story end to end.
- `test_admin_gate.py`: the parametrised gate over every admin route (the route list is
  checked against OpenAPI, so a new admin route without the gate fails).
- `test_audit.py`: one event per mutation, none for a failed mutation or a no-op; UPDATE and
  DELETE refused for the app login; filters and keyset paging.
- `test_rls.py`: extended with `invitations` and `audit_events` and the new path-id routes.

**Web (`web/src/__tests__`):**
- `WorkspacePicker.test.tsx`: lists, switches, sends the header and falls back from a stale
  id.
- `AdminMembers.test.tsx`: invite form, role change, remove with confirmation, and invitation
  resend and revoke.
- `AdminWorkspaces.test.tsx`: create, the access list, and adding a team role.
- `AdminTeams.test.tsx` and `AdminAudit.test.tsx`: the main flows; filters and "Load more".
- `PasskeyGate.test.tsx`: a `second-factor-required` answer shows the gate.

## Implementation notes

Recorded while implementing (3 Oct 2026); the spec above is corrected accordingly.

- **The accept function returns the orgs joined** (`SETOF uuid`), not a count: `/api/auth/me`
  needs the org to move a session without access into it.
- **Workspace admins read the member and team lists.** A workspace admin who is not an org
  admin has to pick org members and teams when granting access to their workspace. Changing
  either list stays with org admins.
- **Error codes.** A workspace role without a workspace, or the reverse, answers
  `invalid_workspace_grant`; an owner or unknown role answers `invalid_role`.
- **Invitation routes** live in `routers/invitations.py` under the same prefix and gate as
  `routers/admin.py`. Inline jobs send the email after the response (the worker's last
  attempt, without retries).
- **Web.** The default workspace offers no Delete, since the API always refuses it. The header
  picker and the invite form's workspace menu carry distinct accessible names. Both were found
  driving the panel in Chromium against the API with an aiosmtpd sink.

## Decisions (owner, 3 Oct 2026)

1. **Join at sign-in.** An invitation is bound to an email and accepted when that verified
   email signs in. The email points to `/login` and carries no secret, so nothing can leak by
   forwarding it. A mistyped address simply never joins, and the admin revokes it.
2. **Email over SMTP** through the existing Google Workspace relay, sent by the worker.
   Without SMTP settings, invitations still work and the admin tells the person.
3. **One organisation per install** in this step. Creating orgs, switching org and
   cross-org invitations are a later spec. The API already keeps every query in the
   session's org.
4. **"View as user" later** (Arqam's read-only impersonation): a follow-up spec.

## Out of scope

- Several organisations per install, org creation and an org switcher: a later spec (with the
  enterprise SSO work, S19).
- "View as user": a follow-up spec in sprint 10 or 11.
- Disabling a user install-wide: an install-admin story. In v1, org admins remove members.
- Shares with users, teams and links: S12-1.
- Auth events (login, logout, revocations) in `audit_events`, and export to a SIEM: spec 015
  or later. They stay in the logs as in spec 013.
- Deleting a workspace together with its data, and data retention: S10 or later.
- Rate limits on the admin and auth routes: spec 015 (S8-6).
- Session refresh (S8-5): already covered by spec 013's sliding idle timeout. The SPA needs no
  token refresh, because the browser holds only the session cookie.
