# Spec 015 — Security baseline, pass 1

Sprint 8, story S8-6. Depends on: 007 (the app login and its startup check), 013 (sign-in
routes, cookies), 014 (admin routes, invitations). Packages: `api/` (rate limits, response
headers, startup refusal, migration 0007), `deploy/caddy/` (edge headers), `web/` (lint rule,
429 text), `.github/workflows/` (dependency audits). Checklist:
`docs/frontend/02-security-baseline.md`.

## Goal

The baseline's transport, header, cookie, rate-limit and supply-chain items hold and are
tested, so sprint 8 can close and release v0.2.0:

- **Headers:** the edge sends HSTS and a stricter CSP. API responses carry their own
  `nosniff`, `no-store` and frame protection, so a direct API call is covered too.
- **Rate limits:** sign-in and admin routes are rate-limited per client IP or per user, and
  answer 429 with `Retry-After`. Counters live in Postgres, so several api replicas share
  them.
- **Dependency audits:** `cargo audit`, `pip-audit` and `pnpm audit` fail CI on a known
  advisory. A weekly run catches advisories published between pull requests.
- **RLS bypass:** in `prod`, the api and the worker refuse to start on a database login
  that bypasses row-level security (exit code 4), as spec 007 announced.

## User story

As the owner of an install exposed to the internet, I want password-spraying and scripted
abuse of sign-in and invitations slowed down, browsers told to use HTTPS only, and known
vulnerable dependencies blocked before they ship, so that the first customer data lands on
a server that passes the baseline.

## Interface

### Edge headers (`deploy/caddy/Caddyfile`, every response of the web app)

| Header | Value |
|---|---|
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` (no `preload`, see Decisions) |
| `Content-Security-Policy` | as today, with `style-src 'self'` (no `'unsafe-inline'`; the build has no inline styles, and React sets styles through the DOM, which CSP allows) |
| `Cross-Origin-Resource-Policy` | `same-origin` |
| `Permissions-Policy` | `camera=(), microphone=(), geolocation=(), payment=(), usb=()` |
| `X-Frame-Options` | `DENY` (for old browsers; `frame-ancestors 'none'` covers current ones) |
| unchanged | `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, `Cross-Origin-Opener-Policy: same-origin`, no `Server` header |

### API response headers (middleware, every `/api` and `/healthz` response)

- `X-Content-Type-Options: nosniff`.
- `Cache-Control: no-store`: API responses carry tenant data and must not sit in shared
  caches.
- `Referrer-Policy: no-referrer`.
- HTML responses (the sign-in failure pages) also get `Content-Security-Policy:
  default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none';
  form-action 'none'` and `X-Frame-Options: DENY`.

### Rate limits

Fixed windows counted in Postgres: table `rate_limits`, migration 0007.

| Column | Type |
|---|---|
| `bucket` | text |
| `key` | text |
| `window_start` | timestamptz |
| `hits` | integer |

The primary key is (`bucket`, `key`, `window_start`). There is no RLS: the table is not
tenant data, and it is read before any org is known. One `INSERT … ON CONFLICT DO UPDATE …
RETURNING hits` per limited request, in its own short transaction.

| Bucket | Routes | Key | Limit |
|---|---|---|---|
| `auth.login` | `GET /api/auth/login` | client IP | 20 per minute |
| `auth.callback` | `GET /api/auth/callback` | client IP | 20 per minute |
| `auth.backchannel` | `POST /api/auth/backchannel-logout` | client IP | 60 per minute |
| `auth.sessions` | `DELETE /api/auth/sessions/{id}`, `POST …/revoke-others`, `POST /api/auth/logout` | session user, else IP | 30 per minute |
| `admin.write` | every `POST`, `PUT`, `PATCH`, `DELETE` under `/api/admin` | user | 60 per minute |
| `admin.invite` | `POST /api/admin/invitations`, `…/resend` | org | 50 per hour |

- The client IP is `X-Real-IP` (set by the proxy, as for sessions in spec 013), else the
  peer address.
- Over the limit, the request answers 429 with `Retry-After: <seconds to the window end>`.
  - Browser navigations (`login`, `callback`) get a short HTML page: "Too many sign-in
    attempts. Try again in N seconds."
  - JSON routes get `{"detail": "rate_limited"}`.
  - A refused request does nothing else, and the event is logged as `rate.limited` with the
    bucket and key.
- `TABAYYUN_RATE_LIMITS`, default `true`: `false` turns every limit off. It is for load
  tests only; `prod` logs a warning when it is off.
- A maintenance job, `tabayyun.prune_rate_limits`, deletes windows older than one day every
  ten minutes, next to the stale-run reaper.

### Startup refusal

`check_login` in `prod` exits the process with code 4 when the database login is a
superuser, has `BYPASSRLS` or owns the tenant tables. The `db.rls_bypassed` log line and its
hint stay as they are. In `dev` and `test` it remains a warning.

### CI and lint

- `ci.yml` makes the audits blocking:
  - `pip-audit --skip-editable` and `pnpm audit --audit-level high` lose `|| true`;
    `cargo audit` already blocks.
  - An accepted advisory goes into the workflow as `--ignore-vuln <ID>` (pip-audit), into
    `pnpm.auditConfig.ignoreCves` (pnpm) or `core/.cargo/audit.toml` (cargo), with a reason
    and a review date, never silently. (Edited during implementation: pip-audit has no
    ignore file, so the flag next to the step is the one place to look.)
- The Rust steps run with `--locked`, as `uv sync --locked` and `pnpm install
  --frozen-lockfile` already do.
- A new `audit.yml` runs the three audits every Monday at 05:00 UTC and on demand.
- An ESLint rule forbids `dangerouslySetInnerHTML` in the web app.

### Web

- The admin panel's error texts gain `rate_limited`: "Too many changes in a short time. Wait
  a minute and try again."

## Behaviour

1. **Edge.** Every response from the web container carries the edge headers. HSTS is
   honoured by browsers only over HTTPS; CapRover terminates TLS in front of Caddy.
2. **API headers.** The middleware adds its headers after the route has answered, error
   responses and 401/403/404/429 included. It never overrides a header the route set.
3. **Counting.**
   - Each limited request increments its window's counter before the route runs. Requests
     over the limit still count, so a client that keeps hammering stays refused until the
     window ends.
   - A database error while counting lets the request through and logs `rate.unavailable`:
     a broken counter must not lock everybody out of sign-in.
4. **Order.** CSRF (spec 013), then the rate limit, then the session and the passkey gate.
   An anonymous flood of admin calls is refused by the session check without counting. An
   `admin.write` count needs a signed-in user.
5. **Startup.** In `prod`, an RLS-bypassing login stops the api and the worker with exit
   code 4 after logging. CapRover keeps restarting the container, and the log names the
   setting to change. Staging already runs on the app login (spec 007), so this changes
   nothing there.
6. **CI.**
   - A dependency with a known advisory fails the pull request: `cargo audit`, `pip-audit`,
     and `pnpm audit` at level high or above.
   - The weekly run fails the same way. GitHub then emails the repository's watchers.

## Acceptance criteria

- [x] The Caddyfile carries the headers in the table; a test parses it and fails when one is
      missing or `'unsafe-inline'` comes back into `script-src` or `style-src`.
- [x] Every API response, including 401, 404, 422 and 429, has `nosniff`, `no-store` and
      `no-referrer`. The sign-in failure pages also have their CSP and `X-Frame-Options`.
- [x] Each bucket answers 429 with `Retry-After` on the request after its limit. The window
      resets, other keys stay unaffected, and an unavailable counter lets requests through.
- [x] `TABAYYUN_RATE_LIMITS=false` disables every limit; prod warns.
- [x] Migration 0007 upgrades and downgrades; the prune job deletes only windows older than a
      day.
- [x] In `prod`, an owner or superuser login makes the api lifespan and the worker exit with
      code 4. The app login starts; `dev` keeps warning.
- [x] CI fails on a known advisory in any of the three ecosystems. `audit.yml` runs weekly.
      Rust builds use `--locked`. ESLint rejects `dangerouslySetInnerHTML`.
- [ ] Staging: `curl -sI https://tabayyun-stg.siralabs.org/` shows HSTS and the CSP, sign-in
      with Google and with a passkey still works, and the admin panel loads without CSP
      errors in the console.
- [x] `02-security-baseline.md`: the transport and header, CORS, inline-script and lockfile
      items are ticked. The audit and rate-limit items are annotated with what is left.

## Test cases

**Unit (`api/tests`):**
- `test_edge_headers.py`: parse `deploy/caddy/Caddyfile`. Every required header is present,
  and neither `script-src` nor `style-src` contains `'unsafe-inline'`.
- `test_rate_limit_rules.py`: window arithmetic and `Retry-After`, the client-IP choice, the
  bucket table.
- `test_settings.py`: `TABAYYUN_RATE_LIMITS` parsing.

**Integration (`api/tests/db`):**
- `test_rate_limits.py`:
  - each bucket's limit and 429 shape, as HTML or JSON;
  - window reset, using an injected clock;
  - separate keys;
  - fail-open on a database error;
  - disabled by the setting;
  - the prune job;
  - migration up and down.
- `test_security_headers.py`: the headers on 200, 401, 404, 422 and 429 responses and on
  the callback failure page.
- `test_app_role.py`: prod exits 4 for the owner login, in the api lifespan and the worker
  entry; dev warns.

**Web:** `AdminMembers.test.tsx` checks the `rate_limited` text.

## Decisions

1. **HSTS without `preload`.** Preloading is a property of the registered domain
   (`siralabs.org` and every subdomain), submitted by its owner. `includeSubDomains` on
   `tabayyun-stg.siralabs.org` covers only that host's own subdomains. Revisit for the
   production domain.
2. **Counters in Postgres, not in memory.** Several api replicas share the limits, as in
   Arqam (story 2.7 prunes its rate-limit rows). A fixed window is enough for abuse limits
   and costs one statement.
3. **Fail open** when counting fails: availability of sign-in beats the limiter.
4. **CSP enforced, not report-only.** The policy has been enforced since the compose bundle
   existed and the SPA has no inline code. Only `style-src` tightens, and the staging check
   covers it.

## Implementation notes

- Caddy passed a client's own `X-Real-IP` on to the api, so a script could have sent a new
  address with every request and never met a per-IP limit. The edge now sets it from
  `{client_ip}`, trusting `X-Forwarded-For` only from private-range proxies (CapRover's nginx)
  and reading it from the right (`trusted_proxies_strict`); `test_edge_headers.py` keeps it.
  Checked with Caddy 2.11 against an echo upstream.
- Through Caddy, API responses carry the edge's `Referrer-Policy:
  strict-origin-when-cross-origin`: Caddy's `header` replaces the api's `no-referrer`. Both
  keep the path and query from other origins; the api's own value matters for direct calls.
- The api's security headers are a pure ASGI middleware outside the CSRF one, so the CSRF
  refusal and unhandled errors get them too. They use `setdefault`: a route's own value wins.
- `admin.write` is counted after the session check (the router's dependency order), so an
  anonymous flood is refused with 401 without writing counters; `admin.invite` counts on top
  of it for the two invitation routes.
- The exit-4 test runs the api's lifespan directly instead of through `LifespanManager`,
  whose task would swallow the `SystemExit` into the event loop.
- Checked in Chromium through Caddy with the strict CSP: runs list, a run report with its
  timeline, the four admin tabs and the account page, with no violation in the console.

## Out of scope

- Auth events (sign-in, sign-out, revocations) in `audit_events`, request IDs and
  OpenTelemetry: the observability pass.
- `cargo deny` (licences), Renovate with a cooldown, cosign signatures: the supply-chain
  pass in sprint 13 (S13-4).
- Per-tenant rate limits on data routes (uploads, runs): sprint 10, with suites.
- The ASVS L2 self-assessment and the pen test: S13-4.
