# Spec 013 — Keycloak realm and OIDC login (backend-for-frontend)

Sprint 8, stories S8-1 and S8-2. Depends on: 007 (users, memberships, `Principal`, RLS).
Packages: `api/` (new `tabayyun.auth`, settings, migration 0005, `authz.deps`), `web/`
(session check, login page, devices, sign-out), `deploy/` (realm export, CapRover and
compose docs).

Status: decisions confirmed by the owner on 2026-09-26. Keycloak stays (ADR-0006). What users
see and the app-side rules follow Arqam's sign-in and admin specs (Arqam specs 008–012,
Better Auth), so the Sīra family behaves alike.

## Goal

A person opens Tabayyun and signs in with Google, GitHub or a passkey. The `tabayyun` realm
in Keycloak does the sign-in: Google and GitHub are brokered identity providers, and
passkeys are Keycloak's WebAuthn passwordless credentials.

- **Tokens:** the API runs the OIDC Authorization Code flow with PKCE and keeps every token
  server-side. The browser gets only an opaque `__Host-tby_session` cookie.
- **Requests:** every API request acts for that user in the user's org, replacing the
  bootstrap principal of spec 007. No valid session → 401. An unsafe request without the
  CSRF header → 403.
- **Sessions:** a session records how it signed in (`google`, `github`, `passkey`), so admin
  actions (spec 014) can require a recent passkey sign-in, as in Arqam. Users see their
  signed-in devices and can sign the others out.
- **Logout:** ends the Tabayyun session and the Keycloak session. Keycloak's back-channel
  logout ends Tabayyun sessions too.
- **Who gets in:** until invitations arrive (spec 014), only `TABAYYUN_ADMIN_EMAIL` gets
  access, as owner of the default org. Everyone else who signs in sees "No access yet".

## User story

As a data engineer at a site, I sign in with Google, GitHub or a passkey so that only I and
the people my organisation admits see our series, findings and runs, and I never have a
Tabayyun password.

## Interface

### Settings (environment variables)

| Name | Default | Meaning |
|---|---|---|
| `TABAYYUN_AUTH_MODE` | `oidc` in `prod`, `dev` otherwise | `oidc`: sessions required. `dev`: every request acts as the bootstrap principal (007 behaviour); refused in `prod` |
| `TABAYYUN_PUBLIC_URL` | — | External base URL, e.g. `https://tabayyun-stg.siralabs.org`; redirect URIs and the `Origin` check derive from it |
| `TABAYYUN_OIDC_ISSUER` | — | e.g. `https://miftachun.apps.data-and-ai-dude.ch/realms/tabayyun`; discovery at `/.well-known/openid-configuration` |
| `TABAYYUN_OIDC_CLIENT_ID` | `tabayyun-api` | Confidential client in the realm |
| `TABAYYUN_OIDC_CLIENT_SECRET` | — | Its secret |
| `TABAYYUN_SIGN_IN_METHODS` | `google,github,passkey` | Buttons the login page shows; each must also be set up in the realm |
| `TABAYYUN_ADMIN_EMAIL` | empty | The first owner (Arqam's `ARQAM_ADMIN_EMAIL` rule, behaviour 3) |
| `TABAYYUN_SESSION_IDLE` | `12h` | Idle timeout (security baseline) |
| `TABAYYUN_SESSION_ABSOLUTE` | `30d` | Absolute lifetime (Arqam: 30 days too) |
| `TABAYYUN_PASSKEY_FRESH` | `12h` | How recent a passkey sign-in must be for admin actions (Arqam spec 012) |

In `prod` with `oidc`, `require_secrets_in_prod` refuses to start without `TABAYYUN_PUBLIC_URL`
(https), `TABAYYUN_OIDC_ISSUER`, `TABAYYUN_OIDC_CLIENT_SECRET`, `TABAYYUN_SESSION_SECRET` and a
non-empty `TABAYYUN_ADMIN_EMAIL`, or with placeholder values. Without an admin email no
login could ever become owner: the migration's bootstrap owner has no login.
`TABAYYUN_SESSION_SECRET` (existing) keys the HMAC of session and login-flow IDs.

### Routes (all under `/api/auth`, none behind `authorize()`)

| Route | Request | Response |
|---|---|---|
| `GET /api/auth/sign-in-options` | — | `{"google": true, "github": true, "passkey": true}` from `SIGN_IN_METHODS` (Arqam's `/v1/sign-in-options`) |
| `GET /api/auth/login?method=google&next=/runs` | `method` ∈ enabled methods; `next`: relative path, default `/` | 302 to the IdP's authorization endpoint (`response_type=code`, `scope=openid email profile`, `state`, `nonce`, `code_challenge` S256). `google`/`github` add `kc_idp_hint`. `passkey` adds `prompt=login`. Sets `__Host-tby_login` (flow ID, 10 min). An unknown or disabled method → 400 |
| `GET /api/auth/callback?code&state` | from the IdP | 302 to `next`; sets `__Host-tby_session`; clears `__Host-tby_login` |
| `GET /api/auth/me` | cookie | 200 with `user` (`id`, `email`, `display_name`), `org` (`id`, `name`), `role` (e.g. `owner`), `sign_in_method` (`google`/`github`/`passkey`) and `passkey_fresh` (bool); 401 without a session; 403 `{"detail": "no_access"}` for a signed-in user without a membership |
| `GET /api/auth/sessions` | cookie | `[{"id", "current", "sign_in_method", "created_at", "last_seen_at", "user_agent", "ip_address"}]`, never tokens |
| `DELETE /api/auth/sessions/{id}` | CSRF header | 204; only the user's own sessions (404 otherwise) |
| `POST /api/auth/sessions/revoke-others` | CSRF header | 204; revokes every other session of the user |
| `POST /api/auth/logout` | CSRF header | 200 `{"logout_url": "<IdP end_session URL with id_token_hint and post_logout_redirect_uri>"}`; clears the cookie; the web app then navigates there |
| `POST /api/auth/backchannel-logout` | form `logout_token` (JWT from the IdP) | 200; 400 on an invalid token. Exempt from the CSRF header, authenticated by the token's signature |
| `GET /api/auth/passkeys` | cookie | 302 to the Keycloak account console's "Signing in" page, where passkeys are added, renamed and removed |

Cookies: `__Host-tby_session` and `__Host-tby_login` are `HttpOnly; Secure; SameSite=Lax;
Path=/`. Their values are random 32-byte tokens (base64url). Only an HMAC-SHA256 of each
(keyed by the session secret) is stored.

The dependency for spec 014: `require_recent_passkey()` → 403 `{"detail":
"second-factor-required"}` unless the session signed in with a passkey less than
`PASSKEY_FRESH` ago (Arqam's admin gate). Spec 014 applies it to every admin route.

### Database (migration 0005)

| Table | Columns | Access for `tabayyun_app` |
|---|---|---|
| `user_identities` | `issuer` text, `subject` text, `user_id` → users, `email_at_login` text, `created_at`, `last_login_at`; PK (`issuer`, `subject`) | SELECT only |
| `sessions` | `id` uuid PK (what devices name), `id_hash` bytea unique, `user_id`, `org_id` (null = no access), `sign_in_method` text CHECK (`google`, `github`, `passkey`), `idp_sid` text, `id_token` text, `ip_address` inet, `user_agent` text, `created_at`, `last_seen_at`, `expires_at`, `revoked_at`; index on `idp_sid`, on `user_id` | SELECT, INSERT, UPDATE (no RLS: looked up by hash before any org is known) |
| `login_flows` | `id_hash` bytea PK, `state` text, `nonce` text, `code_verifier` text, `method` text, `next` text, `created_at` | SELECT, INSERT, DELETE |

`users` stays read-only to the app login (spec 007). The only write path is the SECURITY
DEFINER function `tabayyun_login(issuer, subject, email, email_verified, display_name,
admin_email)`, run as the owner, like the reaper. It returns `(user_id, org_id, role)`, or no
membership. The function:
- links or creates the identity and the user;
- grants `owner` of the default org by the rule in behaviour 3;
- serialises with `pg_advisory_xact_lock(hashtext('tabayyun.admin_bootstrap'))`, as Arqam
  does.

### Keycloak (`deploy/keycloak/tabayyun-realm.json`)

- **Realm and client:** realm `tabayyun`; confidential client `tabayyun-api` (standard flow
  only, PKCE S256 required).
  - URLs: redirect URI `<PUBLIC_URL>/api/auth/callback`, post-logout redirect
    `<PUBLIC_URL>/`, back-channel logout URL `<PUBLIC_URL>/api/auth/backchannel-logout` with
    "session required".
  - Mappers: `identity_provider` (user-session note, the broker alias `google` or `github`)
    and `amr` (Keycloak's authentication-method-reference mapper).
- **Identity providers:**
  - Google: "trust email".
  - GitHub: scope `user:email`, "trust email" (GitHub returns the primary verified email).
  - First-broker-login flow: create the user if the email is new, else link to the existing
    Keycloak user with that email; no review page. Linking is safe because both providers are
    trusted for email and the API refuses `email_verified = false`. This is Arqam's rule: an
    unverified email never links.
- **Passkeys:**
  - Policy: WebAuthn passwordless, discoverable credentials, user verification required,
    relying-party ID = the Keycloak host.
  - Browser flow, in this order: identity-provider redirector (follows `kc_idp_hint`),
    SSO cookie, passkey. There is no password form.
  - The passkey execution carries the AMR reference `passkey` with a maximum age of 120 s, so
    the ID token's `amr` contains `passkey` only right after a passkey sign-in.
- **Account settings:** no self-registration with a password; no password credential type
  at all ("no passwords", as Arqam). Brute-force protection and OTP policy as in the master
  realm.
- **Secrets:** the export contains none. The Google and GitHub client secrets, the client
  secret and SMTP are set in the admin console after import; `deploy/caprover.md` lists the
  steps and the GitHub OAuth app to create
  (`<keycloak>/realms/tabayyun/broker/github/endpoint`).

### Web

- **Session check:** on start the SPA calls `/api/auth/me`.
  - 401 → `/login`: one button per `sign-in-options` ("Continue with Google", "Continue
    with GitHub", "Sign in with a passkey"), each navigating to
    `/api/auth/login?method=…&next=<current path>`.
  - 403 `no_access` → a "No access yet" page naming the signed-in email, with a sign-out
    button.
- **API calls:** a 401 from any API call sends the user to `/login` with the current path.
- **Header:** shows the user's name and a "Sign out" action (`POST /api/auth/logout`, then
  `window.location = logout_url`).
- **Settings → Account:**
  - signed-in devices from `/api/auth/sessions`, with "Sign out" per device and "Sign out
    all other devices";
  - "Manage passkeys", which opens `/api/auth/passkeys`;
  - a hint for admins without a fresh passkey sign-in. The gate itself is spec 014's.

## Behaviour

1. **Startup.**
   - `TABAYYUN_AUTH_MODE=dev` in `prod` stops the start with an error naming the setting, like
     every other `require_secrets_in_prod` refusal.
   - With `oidc`, the api fetches the discovery document lazily on the first login and
     caches it for 1 h, together with the JWKS. When the IdP is unreachable, the login routes
     answer 503 and nothing else is affected.
2. **Login start.**
   - `next` must be a relative path starting with `/`, not `//` and without a scheme; anything
     else becomes `/` (no open redirect).
   - The api creates a flow (state, nonce, PKCE verifier, method), stores it, sets
     `__Host-tby_login` and answers 302.
3. **Callback.**
   - **Flow match:** the `__Host-tby_login` cookie must match a flow younger than 10 minutes
     whose `state` equals the query's; otherwise 400 `login_expired`, with a link to start
     again. The flow is deleted either way (single use).
   - **Code exchange:** the code is exchanged at the token endpoint with the client secret and
     the verifier. An IdP error gives 502 `idp_error`, logged with the IdP's `error` code, never
     the body.
   - **ID token:** it must pass signature (JWKS, RS256/ES256 only), `iss`, `aud`/`azp`, `exp`
     (60 s leeway), `nonce` and `email_verified = true`. Any failure gives 400
     `invalid_token`.
   - **Sign-in method:** the token must prove the method the flow recorded, and the session
     stores that method.
     - `passkey`: `amr` contains `passkey`, else 400 `passkey_required`.
     - `google`/`github`: the `identity_provider` claim equals the method, else 400
       `invalid_token`. `kc_idp_hint` only routes the request, so a user could otherwise
       finish a Google flow through another provider, including one switched off in
       `TABAYYUN_SIGN_IN_METHODS`.
     - Only the claim for the recorded method counts: within one Keycloak session the other
       claim can be stale (see "Implementation notes").
   - **User and membership:** `tabayyun_login(...)` runs with issuer, `sub`, lower-cased email,
     `email_verified`, `name` and `TABAYYUN_ADMIN_EMAIL`. Its admin rule (Arqam's bootstrap
     rule): the user becomes `owner` of the default org only if the email equals
     `TABAYYUN_ADMIN_EMAIL`, it is verified, and the default org has no owner yet besides the
     bootstrap user. Later changes of the setting grant nothing.
   - **Session:** a new session is created, a new ID every login (rotation), with the sign-in
     method, `idp_sid` from the token's `sid`, the ID token (for the logout hint), IP (from
     `X-Real-IP` behind the proxy) and user agent. The api sets `__Host-tby_session` and
     answers 302 to `next`. A user without a membership still gets a session (org null), so
     `/me` can answer `no_access`.
4. **Every other `/api` request** except `/api/version`, `/api/auth/*` and the health
   endpoint:
   - **Session lookup:** the cookie is looked up by HMAC. Missing, unknown, revoked, idle for
     longer than `SESSION_IDLE` or past `expires_at` → 401 `{"detail":
     "not_authenticated"}`.
   - **Principal:** `last_seen_at` is updated at most once a minute. A session without an org
     gets 403 `no_access`. `get_principal` returns `(user_id, org_id)`; `authorize()` and RLS
     then work as in spec 007.
   - **Disabled user:** `users.disabled_at` set → 401, and every session of the user is
     revoked (Arqam: a ban deletes all sessions).
5. **CSRF, for every `POST`, `PUT`, `PATCH`, `DELETE`** except the back-channel logout:
   - the `X-Tabayyun-Request: 1` header is required;
   - an `Origin` header, when present, must equal `PUBLIC_URL`'s origin;
   - otherwise 403 `csrf`.

   This is stricter than Arqam (SameSite plus origin only), deliberately: Tabayyun takes
   multipart uploads, and the web app already sends the header.
6. **Devices:** users see only their own sessions. Revoking the current session behaves like
   logout without the IdP round trip.
7. **Logout:** revokes the session, clears the cookie and returns the IdP's `end_session`
   URL with `id_token_hint` and `post_logout_redirect_uri`. Without a session it still
   answers 200 with the plain end-session URL.
8. **Back-channel logout:**
   - **Token check:** the `logout_token` must pass signature, `iss`, `aud` and `iat`, carry
     the back-channel event claim, and carry no `nonce`.
   - **Revocation:** every session with its `sid` (or, without `sid`, every session of the
     identity's `sub`) is revoked.
9. **Passkey freshness:** `require_recent_passkey()` checks the current session's
   `sign_in_method = 'passkey'` and `created_at` within `PASSKEY_FRESH`.
   - Unlike Arqam, the rule does not depend on whether the admin already has a passkey. The
     app cannot see Keycloak's credentials without an admin API call, so every admin needs
     one. The gate tells an admin without one to add it under "Manage passkeys" first.
   - Recovery for an admin who lost every passkey is an operator step: remove the WebAuthn
     credential in Keycloak's admin console after an out-of-band identity check. This matches
     Arqam's operator step; `deploy/caprover.md` documents it.
10. **`dev` mode:** unchanged from spec 007 (bootstrap principal, no cookie needed), so local
    work and the existing tests keep running. `/api/auth/me` answers with the bootstrap user
    and `sign_in_method` `dev`. The CSRF check applies in both modes.
11. **Logging:** logins, logouts, session revocations and rejections are logged as
    `auth.login`, `auth.logout`, `auth.session_revoked`, `auth.rejected` with user ID, method
    and reason, never tokens, codes or email bodies. The audit table (Arqam's `audit_log`
    shape) arrives with spec 014.

## Acceptance criteria

- [ ] With a mocked IdP (discovery, JWKS, token endpoint), the full code+PKCE flow sets a
      session cookie for each method: `google` and `github` via `identity_provider`,
      `passkey` via `amr`. `/api/auth/me` returns the user and the method; an API list call
      returns their org's rows.
- [ ] State mismatch, a reused flow, an expired flow, a wrong nonce, a wrong audience, a bad
      signature, `alg=none`, `email_verified=false`, a passkey flow without `passkey` in `amr` and
      a Google flow that returns through GitHub each fail with the stated status, and no
      session is created.
- [ ] `next=https://evil.example` and `next=//evil.example` redirect to `/`. A disabled
      `method` gives 400.
- [ ] `TABAYYUN_ADMIN_EMAIL` becomes owner at the first verified login, only while the
      default org has no other owner; a second login or a changed setting grants nothing.
      Any other email gets a session and a 403 `no_access` from `/me` and from API routes.
- [ ] Unknown, revoked, idle-expired and absolute-expired sessions each give 401. A disabled
      user gives 401 and loses every session.
- [ ] `GET /api/auth/sessions` lists only the user's sessions without tokens. Deleting another
      user's session gives 404. `revoke-others` keeps only the current one.
- [ ] Unsafe requests without `X-Tabayyun-Request` or with a foreign `Origin` give 403. The
      back-channel logout is exempt.
- [ ] Logout revokes the session and returns an end-session URL with `id_token_hint`. A
      back-channel logout token revokes the matching sessions; an invalid one gives 400.
- [ ] `require_recent_passkey()` passes a passkey session younger than 12 h. It refuses an
      older one, and a Google or GitHub one, with 403 `second-factor-required`.
- [ ] `prod` refuses `AUTH_MODE=dev`, missing or placeholder OIDC settings, and an empty
      `TABAYYUN_ADMIN_EMAIL`.
- [ ] RLS still isolates: a session of org A cannot read org B's rows (007's cross-tenant test
      with real sessions instead of the dependency override).
- [ ] The web app:
      - shows the enabled sign-in buttons;
      - redirects to `/login` when signed out;
      - shows "No access yet" for 403;
      - lists and revokes devices;
      - signs out through the IdP.
- [ ] Staging: the owner signs in with Google, with GitHub and with a passkey on
      `tabayyun-stg.siralabs.org`, sees the example data, sees the three sessions under
      devices and signs out. An anonymous `curl` of `/api/series` gives 401.
- [ ] `docs/frontend/02-security-baseline.md`: the authentication and session items and the
      CSRF item are ticked.

## Test cases

Unit (`api/tests/auth`):
- `test_next_param_is_relative_only`
- `test_login_flow_is_single_use`
- `test_id_token_validation` (parametrised over each failure above)
- `test_sign_in_method_from_claims`
- `test_session_hmac_lookup`
- `test_idle_and_absolute_expiry`
- `test_csrf_header_and_origin`
- `test_require_recent_passkey`
- `test_prod_refuses_dev_mode`

Integration (`api/tests/db/test_auth_flow.py`, next to the database fixtures of spec 007), with a
fake IdP served in
process (discovery, JWKS with a generated RSA key, token endpoint, end-session URL):
- `test_full_login_per_method`
- `test_admin_email_becomes_owner_once`
- `test_unknown_email_gets_no_access`
- `test_devices_list_and_revoke`
- `test_logout_revokes_and_returns_end_session`
- `test_backchannel_logout_revokes_by_sid`
- `test_disabled_user_loses_sessions`
- `test_cross_tenant_isolation_with_sessions`
- `test_tabayyun_login_function_is_the_only_users_write_path` (the app login cannot insert
  into `users` directly)

Web (`web/src/__tests__`):
- `Login.test.tsx`: buttons follow `sign-in-options`; 401 redirects to `/login` with `next`.
- `NoAccess.test.tsx`: the 403 page.
- `Devices.test.tsx`: list, revoke one, revoke others.
- `SignOut.test.tsx`: logout, then navigation to `logout_url`.

## Decisions (confirmed 2026-09-26)

1. **Keycloak stays (ADR-0006).** Per-organisation enterprise SSO (Entra ID, Okta, SAML) and
   SCIM are on Tabayyun's roadmap (S19). Tabayyun's API is Python, where Better Auth does not
   run. User-facing behaviour follows Arqam instead:
   - sign-in with Google, GitHub and passkeys, no passwords;
   - the sign-in-options endpoint;
   - the admin bootstrap by one verified email;
   - linking only by verified email;
   - devices;
   - the 12-hour passkey gate for admins;
   - recovery as an operator step.
2. **Who gets in before spec 014:** only `TABAYYUN_ADMIN_EMAIL`. Everyone else sees "No
   access yet". This differs from Arqam's open sign-up on purpose: Tabayyun holds
   organisations' operational data.
3. **joserfc** (Authlib's JOSE library) for JWKS and JWT validation, and httpx for discovery
   and the token endpoint. Authlib 1.8 deprecates its own `authlib.jose` and its httpx client
   in favour of these, so depending on Authlib itself would add nothing (see implementation
   notes).
4. **Sessions in Postgres**, as in Arqam; no Redis.
5. **One realm per install:** staging uses realm `tabayyun` on the current Keycloak;
   production gets its own Keycloak on the production server (ADR-0016).
   - Passkeys are bound to the Keycloak host (the relying-party ID), so staging passkeys do
     not work on production, and a Keycloak host change invalidates them. The production
     Keycloak should get its final host name before the first passkey is registered.
6. **No magic link:** Keycloak has no built-in email-link sign-in. Google, GitHub and passkeys
   cover sign-in; revisit if a customer needs email-only sign-in.

## Implementation notes

Recorded while implementing (2026-09-26); the spec above is corrected accordingly.

- **Passkey proof via `amr`, not `acr`.** Checked with Keycloak 26.4.7 in a container, with a
  virtual authenticator in Chromium: register a passkey, then sign in with it only.
  - Keycloak's AMR mapper reports the reference configured on the passkey execution
    (`amr: ["passkey"]`).
  - A level-of-assurance step-up (`acr_values`) would have needed conditional sub-flows and
    does not add anything here, so the login asks for no `acr`.
- **Stale claims within one Keycloak session.** Measured with a second realm standing in for
  Google (alias `google`):
  - a passkey sign-in after a Google sign-in still carries `identity_provider: google`;
  - a Google sign-in shortly after a passkey sign-in still carries `amr: ["passkey"]`.

  Hence the callback checks only the claim of the method it asked for, and the passkey
  reference expires after 120 s instead of 12 h.
- **Flow order.** With the SSO cookie first, `kc_idp_hint` was ignored while a Keycloak
  session existed, and `prompt=login` turned "Continue with Google" into a passkey
  re-authentication. The identity-provider redirector now runs first. Only the passkey
  method sends `prompt=login`.
- **First broker login.** "Detect existing broker user" with "Automatically set existing
  user" failed with `invalid_user_credentials`. "Create user if unique" or "Automatically set
  existing user", as alternatives, links an existing email and creates a new one (checked
  with the fake Google realm).
- **Callback failures are pages.** The callback is a browser navigation, so its failures
  answer with the stated status and a short HTML page naming the code (`login_expired`,
  `invalid_token`, `passkey_required`, `idp_error`, `idp_unavailable`) and linking to
  `/login`. Two more codes: `login_cancelled` when the IdP returns `access_denied`, and 403
  `account_disabled` for a disabled user, who gets no session.
- **`no_access` names the email.** `/me`'s 403 body is `{"detail": "no_access", "email": ...}`,
  so the "No access yet" page can say who is signed in; API routes answer `no_access` alone.
- **Every other router needs a principal.** `create_app` attaches `get_principal` to every
  router but `/api/auth`, so a route without `authorize()` (the stateless check run) is not
  anonymous either.
- **Checked against Keycloak 26.4.** The api in `oidc` mode against the realm in a container,
  in Chromium with a virtual authenticator: a passkey login and a "Continue with Google" login
  through a second realm standing in for Google, both linked to one user who became owner;
  the `__Host-` cookie flags; the device list; logout through the end-session URL; and a
  back-channel logout sent by Keycloak when an admin ended the user's sessions.
- **Placeholder URL.** The realm export carries `__PUBLIC_URL__`; `deploy/keycloak/render.py
  <url>` fills it for an install. `make dev-infra` renders it for `http://localhost:5173`,
  and the dev Keycloak (now 26.4) imports it at start.

## Out of scope

- Organisations, workspaces, members, teams, invitations, the org picker and the admin panel:
  spec 014 (S8-4, S8-5). It follows Arqam specs 011–012:
  - user search, roles with a last-owner guard, disabling users;
  - read-only, audited "view as user" with a banner and a notice email;
  - an append-only `audit_log` written in the same transaction;
  - `require_recent_passkey()` on every admin route.
- Rate limits on auth routes, security headers, dependency audit, refusing an RLS-bypassing
  login in prod: spec 015 (S8-6).
- Enterprise SSO per organisation (Entra ID, Okta, SAML) and SCIM: S19.
- API tokens for scripts and connectors: R3. Until then, scripts such as
  `deploy/examples/seed.py` run against installs in `dev` mode or with a session cookie.
- Production Keycloak and its GitHub and Google OAuth apps: owner tasks (ADR-0016,
  TASKS.md).
