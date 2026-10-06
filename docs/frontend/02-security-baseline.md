# Security baseline (OWASP ASVS 5.0 Level 2 target)

This is the checklist the first release must pass. Each item maps to a CI gate, a code
review item, or an ops runbook entry.

## Authentication and sessions
- [x] OIDC Authorization Code + PKCE only; `state` and `nonce` verified; ID token signature and `aud` validated. (Spec 013: `tabayyun.auth.oidc`.)
- [x] Session: server-side store, opaque ID, `__Host-tby_session`, `HttpOnly; Secure; SameSite=Lax; Path=/`. (Spec 013: Postgres `sessions`, HMAC of the token at rest.)
- [x] Rotate session on login and privilege change; idle timeout 12 h, absolute 30 d (org-configurable); server-side revocation; IdP back-channel logout handled. (Spec 013; the timeouts are per install. Spec 014: roles are read on every request, so a role change applies at once, and removing a member revokes their sessions in the org.)
- [ ] MFA and passkeys enforced per organisation via the IdP. (Spec 013 offers passkeys and `require_recent_passkey()`; spec 014 requires a passkey sign-in from the last 12 h on every `/api/admin` route; per-organisation enforcement for all users later.)
- [ ] API tokens (for integrations) are random, hashed at rest, scoped to a workspace and role, expiring, revocable.

## Authorization
- [x] Single `authorize()` path; deny by default; `visible_ids()` for lists. (Spec 007: `tabayyun.authz`, `visible_workspaces()`.)
- [x] Postgres RLS on all tenant tables; API DB role is not table owner; context set per request and per job. (Spec 007; the live install switches logins per `deploy/caprover.md`, and spec 015 refuses an owner login in prod.)
- [x] Cross-tenant tests for every endpoint; inaccessible → 404. (Spec 007: `api/tests/db/test_rls.py`.)
- [ ] UUIDv7 identifiers; no sequential IDs exposed.

## Input handling
- [ ] Pydantic v2 strict models on every endpoint; size limits on bodies and uploads; pagination caps.
- [x] Connector URLs validated against an allow-list and private-range rules (SSRF); DNS re-resolution pinned; redirects disabled. (Spec 021: `NetPolicy` checks every resolved address. Loopback, link-local and metadata addresses are always refused, private ranges are refused outside `TABAYYUN_CONNECTOR_ALLOWED_NETWORKS`, and public ones optionally. HTTP clients connect to the checked address, with Host and SNI kept, and do not follow redirects.)
- [ ] SQL only via SQLAlchemy parameters; DataFusion sessions read-only with timeouts; user SQL cannot reach Postgres.
- [ ] File uploads: type sniffing, size cap, stored outside web root, never executed.

## Transport and headers
- [ ] TLS 1.2+ at the edge; HSTS with preload; HTTP → HTTPS redirect. (Spec 015: Caddy sends `Strict-Transport-Security: max-age=31536000; includeSubDomains`; CapRover terminates TLS and redirects. `preload` waits for the production domain, whose owner submits it.)
- [x] CSP: `default-src 'self'; script-src 'self' 'sha256-…' 'strict-dynamic'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'` (share embeds get their own policy); report-only first, then enforce. (Spec 015: enforced at the edge with `script-src 'self'` and `style-src 'self'`, no `'unsafe-inline'`, since the SPA has no inline code, so neither hashes nor `'strict-dynamic'` are needed; `test_edge_headers.py` keeps it so. The API's HTML pages carry `default-src 'none'`. Share embeds get theirs with sharing.)
- [x] `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy` minimal, COOP `same-origin`. (Spec 015: at the edge, with CORP `same-origin` and `X-Frame-Options: DENY`; API responses also carry `nosniff`, `Cache-Control: no-store` and `Referrer-Policy: no-referrer` themselves.)
- [x] CORS disabled (same origin) except documented API-token clients. (The api has no CORS middleware, so browsers refuse cross-origin reads; API-token clients come with API tokens.)

## CSRF and XSS
- [x] Unsafe methods require the `X-Tabayyun-Request` header; `Origin` checked when present. (Spec 013: `CsrfMiddleware`. The item first also asked for a JSON content type; uploads are multipart, so the header, which a cross-site form cannot send, is the guard.)
- [x] No inline scripts; no `dangerouslySetInnerHTML`; DOMPurify for markdown. (Spec 015: the CSP refuses inline scripts and styles, and ESLint rejects `dangerouslySetInnerHTML`. Nothing renders markdown yet; DOMPurify comes with the first renderer.)

## Secrets and configuration
- [ ] No secrets in code, images or config files; environment variables or mounted secret files only; startup refuses placeholder secrets.
- [x] Connector credentials encrypted at rest with a KMS-style envelope key (`TABAYYUN_MASTER_KEY` from secret store); write-only in the UI; rotation supported. (Spec 021: AES-256-GCM, bound to their source by the associated data. The API never returns them, and they appear in no log or audit event. `TABAYYUN_MASTER_KEY_PREVIOUS` plus `python -m tabayyun.secrets rotate` rotate the key. The S9-4 pages show only whether credentials are set.)
- [ ] Licence keys are Ed25519-signed; the public key is embedded; private key never leaves the vendor.

## Supply chain and build
- [x] Lockfiles committed; `pnpm install --frozen-lockfile`, `uv sync --locked`, `cargo` with `Cargo.lock`. (Spec 015: the Rust and maturin steps run with `--locked`.)
- [ ] `cargo audit` + `cargo deny` (licences, advisories), `pip-audit`, `npm audit`/Socket in CI; Renovate with cooldown. (Spec 015: `cargo audit`, `pip-audit` and `pnpm audit --audit-level high` block CI, and `audit.yml` runs them every Monday; `cargo deny` and a cooldown come in S13-4.)
- [ ] SBOM (CycloneDX) for each image; images signed with cosign; Trivy scan gate.
- [ ] Reproducible builds for the Rust wheel; wheels built on manylinux with pinned toolchain.

## Multi-tenancy and data
- [ ] `org_id` on every tenant table and every cache path prefix; per-tenant rate limits; tenant-scoped job context. (Spec 015: sign-in routes are limited per client IP, admin writes per user and invitations per org, counted in Postgres; data routes get theirs with suites in sprint 10.)
- [ ] Backups: nightly Postgres dump + cache snapshot; restore drill documented and tested.
- [ ] Data retention configurable per workspace (findings, metrics, cache, audit).

## Logging, audit, monitoring
- [ ] Append-only `audit_events` for auth, authz, share, credential and threshold changes, and link views; export to SIEM via webhook/syslog. (Spec 014: the table, append-only for the app login, with every membership, role, team, workspace and invitation change; auth events, shares, credentials, thresholds and the SIEM export later.)
- [ ] Structured logs without PII or secrets; request IDs propagated; OpenTelemetry traces.
- [ ] Alerts on auth failures spike, RLS policy errors, job failures.

## Process
- [ ] Threat model kept in `docs/architecture/threat-model.md` (STRIDE per component) and reviewed each release.
- [ ] Monthly dependency and base-image patch window; security release process documented; `SECURITY.md` with disclosure contact.
- [ ] Pen test before first paying customer; ASVS L2 self-assessment checked into repo.
