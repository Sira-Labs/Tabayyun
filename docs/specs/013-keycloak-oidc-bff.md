# Spec 013 — Keycloak realm and OIDC login (backend-for-frontend)

Sprint 8, stories S8-1 and S8-2. Depends on: 007 (users, memberships, `Principal`, RLS).
Packages: `api/` (new `tabayyun.auth`, settings, migration 0005, `authz.deps`), `web/`
(session check, login and logout), `deploy/` (realm export, CapRover and compose docs).

Status: **draft for review** (2026-09-26). The decisions marked *(confirm)* are open.

## Goal

A person opens Tabayyun, clicks "Sign in with Google", goes through the `tabayyun` realm in
Keycloak (Google as a brokered identity provider) and comes back logged in. The API runs the
OIDC Authorization Code flow with PKCE, keeps every token server-side, and gives the browser
only an opaque `__Host-tby_session` cookie. Every API request then acts for that user in that
user's org, replacing the bootstrap principal of spec 007. Requests without a valid session
get 401. Unsafe requests without the CSRF header get 403. Logout ends the Tabayyun session
and the Keycloak session. Keycloak's back-channel logout ends Tabayyun sessions too. Until
invitations arrive (spec 014), only the emails in `TABAYYUN_BOOTSTRAP_ADMINS` get access, as
owners of the default org. Everyone else who signs in sees "no access yet".

## User story

As a data engineer at a site, I sign in with my Google account so that only I and the people
my organisation admits see our series, findings and runs, and I never have a Tabayyun
password.

## Interface

### Settings (environment variables)

| Name | Default | Meaning |
|---|---|---|
| `TABAYYUN_AUTH_MODE` | `oidc` in `prod`, `dev` otherwise | `oidc`: sessions required. `dev`: every request acts as the bootstrap principal (007 behaviour); refused in `prod` |
| `TABAYYUN_PUBLIC_URL` | — | External base URL, e.g. `https://tabayyun-stg.siralabs.org`; redirect URIs and the `Origin` check derive from it |
| `TABAYYUN_OIDC_ISSUER` | — | e.g. `https://miftachun.apps.data-and-ai-dude.ch/realms/tabayyun`; discovery at `/.well-known/openid-configuration` |
| `TABAYYUN_OIDC_CLIENT_ID` | `tabayyun-api` | Confidential client in the realm |
| `TABAYYUN_OIDC_CLIENT_SECRET` | — | Its secret |
| `TABAYYUN_OIDC_IDP_HINT` | `google` | Sent as `kc_idp_hint`, so Keycloak goes straight to Google; empty shows Keycloak's page *(confirm)* |
| `TABAYYUN_BOOTSTRAP_ADMINS` | empty | Comma-separated emails that become `owner` of the default org at their first login *(confirm)* |
| `TABAYYUN_SESSION_IDLE` | `12h` | Idle timeout (security baseline) |
| `TABAYYUN_SESSION_ABSOLUTE` | `30d` | Absolute lifetime |

In `prod` with `oidc`, `require_secrets_in_prod` refuses to start without `PUBLIC_URL`,
`OIDC_ISSUER`, `OIDC_CLIENT_SECRET` and `SESSION_SECRET`, or with placeholder values.
`TABAYYUN_SESSION_SECRET` (existing) keys the HMAC of session and login-flow IDs.

### Routes (all under `/api/auth`, none behind `authorize()`)

| Route | Request | Response |
|---|---|---|
| `GET /api/auth/login?next=/runs` | `next`: relative path, default `/` | 302 to the IdP's authorization endpoint (`response_type=code`, `scope=openid email profile`, `state`, `nonce`, `code_challenge` S256, `kc_idp_hint`); sets `__Host-tby_login` (flow ID, 10 min) |
| `GET /api/auth/callback?code&state` | from the IdP | 302 to `next`; sets `__Host-tby_session`; clears `__Host-tby_login` |
| `POST /api/auth/logout` | CSRF header | 200 `{"logout_url": "<IdP end_session URL with id_token_hint and post_logout_redirect_uri>"}`; clears the cookie; the web app then navigates there |
| `POST /api/auth/backchannel-logout` | form `logout_token` (JWT from the IdP) | 200; 400 on an invalid token. Exempt from the CSRF header, authenticated by the token's signature |
| `GET /api/auth/me` | cookie | 200 `{"user": {"id", "email", "display_name"}, "org": {"id", "name"}, "role": "owner"}`; 401 without a session; 403 `{"detail": "no_access"}` for a signed-in user without any membership |

Cookies: `__Host-tby_session` and `__Host-tby_login` are `HttpOnly; Secure; SameSite=Lax;
Path=/`. Their values are random 32-byte tokens (base64url). Only an HMAC-SHA256 of each
(keyed by the session secret) is stored.

### Database (migration 0005)

| Table | Columns | Access for `tabayyun_app` |
|---|---|---|
| `user_identities` | `issuer` text, `subject` text, `user_id` → users, `email_at_login` text, `created_at`, `last_login_at`; PK (`issuer`, `subject`) | SELECT only |
| `sessions` | `id_hash` bytea PK, `user_id`, `org_id`, `idp_sid` text, `id_token` text, `created_at`, `last_seen_at`, `expires_at`, `revoked_at`; index on `idp_sid`, on `user_id` | SELECT, INSERT, UPDATE (no RLS: looked up by hash before any org is known) |
| `login_flows` | `id_hash` bytea PK, `state` text, `nonce` text, `code_verifier` text, `next` text, `created_at` | SELECT, INSERT, DELETE |

`users` stays read-only to the app login (spec 007). The only write path is the SECURITY
DEFINER function `tabayyun_login(issuer, subject, email, display_name, bootstrap_admins
text[])`, run as the owner, like the reaper. It returns `(user_id, org_id, role)` or no
membership. It links or creates the identity and the user, and grants `owner` of the default
org when the email is in the list and the user has no membership yet.

### Keycloak (`deploy/keycloak/tabayyun-realm.json`)

- **Realm and client:** realm `tabayyun`; confidential client `tabayyun-api` (standard flow
  only, PKCE S256 required).
- **Client URLs:** redirect URI `<PUBLIC_URL>/api/auth/callback`, post-logout redirect
  `<PUBLIC_URL>/`, back-channel logout URL `<PUBLIC_URL>/api/auth/backchannel-logout` with
  "session required".
- **Identity provider:** Google, with "trust email", first-broker-login flow creating the
  user without a review page.
- **Account settings:** no self-registration with a password, verified email required;
  brute-force protection and OTP policy as in the master realm.
- **Secrets:** the export contains none (the Google client ID and secret, the client secret
  and SMTP are set in the admin console after import); `deploy/caprover.md` lists the steps.

### Web

- **Session check:** on start the SPA calls `/api/auth/me`.
  - 401 → `/login`, a page with one button that navigates to `/api/auth/login?next=<current
    path>`.
  - 403 `no_access` → a "No access yet" page naming the signed-in email, with a logout button.
- **API calls:** a 401 from any API call sends the user to `/login` with the current path.
- **Header:** shows the user's name and a "Sign out" action (`POST /api/auth/logout`, then
  `window.location = logout_url`).

## Behaviour

1. **Startup.**
   - `TABAYYUN_AUTH_MODE=dev` in `prod` exits with code 2 and names the setting.
   - With `oidc`, the api fetches the discovery document lazily on the first login and
     caches it for 1 h, together with the JWKS. When the IdP is unreachable, the login routes
     answer 503 and nothing else is affected.
2. **Login start.**
   - `next` must be a relative path starting with `/`, not `//` and without a scheme; anything
     else becomes `/` (no open redirect).
   - The api creates a flow (state, nonce, PKCE verifier), stores it, sets `__Host-tby_login`
     and answers 302.
3. **Callback.**
   - **Flow match:** the `__Host-tby_login` cookie must match a flow younger than 10 minutes
     whose `state` equals the query's; otherwise 400 `login_expired`, with a link to start
     again. The flow is deleted either way (single use).
   - **Code exchange:** the code is exchanged at the token endpoint with the client secret and
     the verifier. An error answer from the IdP gives 502 `idp_error`, logged with the IdP's
     `error` code, never the response body.
   - **ID token:** it must pass signature (JWKS, RS256/ES256 only), `iss`, `aud`/`azp`, `exp`
     (60 s leeway), `nonce` and `email_verified = true`. Any failure gives 400 `invalid_token`.
   - **User and membership:** `tabayyun_login(...)` runs with issuer, `sub`, email
     (lower-cased) and `name`.
     - A user without a membership still gets a session (org null), so `/me` can say
       `no_access`.
   - **Session:** a new session is created, a new ID every login (rotation), with `idp_sid`
     from the token's `sid` and the ID token kept for the logout hint. The api sets
     `__Host-tby_session` and answers 302 to `next`.
4. **Every other `/api` request** except `/api/version`, `/api/auth/*` and the health
   endpoint:
   - **Session lookup:** the cookie is looked up by HMAC. Missing, unknown, revoked, idle for
     longer than `SESSION_IDLE` or past `expires_at` → 401 `{"detail": "not_authenticated"}`.
   - **Principal:** `last_seen_at` is updated at most once a minute. The principal is
     `(user_id, org_id)` of the session; a session without an org gets 403 `no_access`.
     `get_principal` returns it; `authorize()` and RLS then work as in spec 007.
   - **Disabled user:** `users.disabled_at` set → 401, and the session is revoked.
5. **CSRF, for every `POST`, `PUT`, `PATCH`, `DELETE`** except the back-channel logout:
   - the `X-Tabayyun-Request: 1` header is required;
   - an `Origin` header, when present, must equal `PUBLIC_URL`'s origin;
   - otherwise 403 `csrf`.

   Multipart uploads (`POST /api/runs`) keep working: the web app already sends the header.
6. **Logout:** revokes the session, clears the cookie and returns the IdP's `end_session`
   URL with `id_token_hint` and `post_logout_redirect_uri`. Without a session it still
   answers 200 with the plain end-session URL.
7. **Back-channel logout:**
   - **Token check:** the `logout_token` must pass signature, `iss`, `aud` and `iat`, carry
     the back-channel event claim, and carry no `nonce`.
   - **Revocation:** every session with its `sid` (or, without `sid`, every session of the
     identity's `sub`) is revoked.
8. **`dev` mode:** unchanged from spec 007 (bootstrap principal, no cookie needed), so local
   work and the existing tests keep running. The CSRF check applies in both modes.
9. **Logging:** logins, logouts and rejections are logged as `auth.login`, `auth.logout`,
   `auth.rejected` with user ID and reason, never tokens, codes or email bodies. The audit
   table arrives with spec 014.

## Acceptance criteria

- [ ] With a mocked IdP (discovery, JWKS, token endpoint), the full code+PKCE flow sets a
      session cookie; `/api/auth/me` returns the user; an API list call returns their org's
      rows.
- [ ] State mismatch, a reused flow, an expired flow, a wrong nonce, a wrong audience, a bad
      signature, `alg=none` and `email_verified=false` each fail with the stated status, and
      no session is created.
- [ ] `next=https://evil.example` and `next=//evil.example` redirect to `/`.
- [ ] An email in `TABAYYUN_BOOTSTRAP_ADMINS` becomes owner of the default org at first login.
      Any other email gets a session and a 403 `no_access` from `/me` and from API routes.
- [ ] Unknown, revoked, idle-expired and absolute-expired sessions each give 401. A disabled
      user gives 401 and a revoked session.
- [ ] Unsafe requests without `X-Tabayyun-Request` or with a foreign `Origin` give 403. The
      back-channel logout is exempt.
- [ ] Logout revokes the session and returns an end-session URL with `id_token_hint`.
- [ ] A back-channel logout token revokes the matching sessions; an invalid one gives 400.
- [ ] `prod` refuses `AUTH_MODE=dev` and missing or placeholder OIDC settings.
- [ ] RLS still isolates: a session of org A cannot read org B's rows (007's cross-tenant test
      with real sessions instead of the dependency override).
- [ ] The web app redirects to `/login` when signed out, shows "No access yet" for 403, and
      signs out through the IdP.
- [ ] Staging: the owner signs in with Google on `tabayyun-stg.siralabs.org`, sees the example
      data and signs out; an anonymous `curl` of `/api/series` gives 401.
- [ ] `docs/frontend/02-security-baseline.md`: the authentication and session items and the
      CSRF item are ticked.

## Test cases

Unit (`api/tests/auth`):
- `test_next_param_is_relative_only`
- `test_login_flow_is_single_use`
- `test_id_token_validation` (parametrised over each failure above)
- `test_session_hmac_lookup`
- `test_idle_and_absolute_expiry`
- `test_csrf_header_and_origin`
- `test_prod_refuses_dev_mode`

Integration (`api/tests/auth`, database fixtures of spec 007), with a fake IdP served in
process (discovery, JWKS with a generated RSA key, token endpoint, end-session URL):
- `test_full_login_sets_session_and_principal`
- `test_bootstrap_admin_becomes_owner`
- `test_unknown_email_gets_no_access`
- `test_logout_revokes_and_returns_end_session`
- `test_backchannel_logout_revokes_by_sid`
- `test_cross_tenant_isolation_with_sessions`
- `test_tabayyun_login_function_is_the_only_users_write_path` (the app login cannot insert
  into `users` directly)

Web (`web/src/__tests__`):
- `Login.test.tsx`: 401 redirects to `/login` with `next`.
- `NoAccess.test.tsx`: the 403 page.
- `SignOut.test.tsx`: logout then navigation to `logout_url`.

## Decisions to confirm before implementation

1. **Who gets in before spec 014:** only `TABAYYUN_BOOTSTRAP_ADMINS` (owners of the default
   org); everyone else sees "No access yet". The alternative, open sign-up as viewer of the
   default org, would let any Google user read staging's data.
2. **OIDC library: Authlib** (BSD-3; client, PKCE, discovery, JWKS and JWT validation in one
   maintained package). The alternative is `joserfc` plus `httpx` by hand: fewer
   dependencies, more code to get right.
3. **Session store:** Postgres (the `sessions` table), no Redis. One indexed lookup per
   request; `last_seen_at` is written at most once a minute.
4. **Straight to Google:** `kc_idp_hint=google` skips Keycloak's page. Empty would show it,
   for when more identity providers exist.
5. **One realm per install:** staging uses realm `tabayyun` on the current Keycloak
   (`miftachun.apps.data-and-ai-dude.ch`). Production gets its own Keycloak on the production
   server (ADR-0016), with the same realm export and its own secrets.

## Out of scope

- Organisations, workspaces, members, teams, invitations, org picker, admin panel and audit
  events: spec 014 (S8-4, S8-5).
- Rate limits on auth routes, security headers, dependency audit, refusing an RLS-bypassing
  login in prod: spec 015 (S8-6).
- Enterprise SSO per organisation (Entra ID, Okta, SAML) and SCIM: S19.
- API tokens for scripts and connectors: R3. Until then, scripts such as
  `deploy/examples/seed.py` run against installs in `dev` mode or with a session cookie.
- MFA and passkey policy: configured in Keycloak, not in Tabayyun.
- Production Keycloak: an owner task (ADR-0016, TASKS.md).
