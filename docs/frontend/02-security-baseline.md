# Security baseline (OWASP ASVS 5.0 Level 2 target)

This is the checklist the first release must pass. Each item maps to a CI gate, a code
review item, or an ops runbook entry.

## Authentication and sessions
- [x] OIDC Authorization Code + PKCE only; `state` and `nonce` verified; ID token signature and `aud` validated. (Spec 013: `tabayyun.auth.oidc`.)
- [x] Session: server-side store, opaque ID, `__Host-tby_session`, `HttpOnly; Secure; SameSite=Lax; Path=/`. (Spec 013: Postgres `sessions`, HMAC of the token at rest.)
- [x] Rotate session on login and privilege change; idle timeout 12 h, absolute 30 d (org-configurable); server-side revocation; IdP back-channel logout handled. (Spec 013; the timeouts are per install. Role changes arrive with spec 014, which revokes the member's sessions.)
- [ ] MFA and passkeys enforced per organisation via the IdP. (Spec 013 offers passkeys and `require_recent_passkey()`; spec 014 applies it to admins; per-organisation enforcement later.)
- [ ] API tokens (for integrations) are random, hashed at rest, scoped to a workspace and role, expiring, revocable.

## Authorization
- [x] Single `authorize()` path; deny by default; `visible_ids()` for lists. (Spec 007: `tabayyun.authz`, `visible_workspaces()`.)
- [x] Postgres RLS on all tenant tables; API DB role is not table owner; context set per request and per job. (Spec 007; the live install switches logins per `deploy/caprover.md`, and spec 015 refuses an owner login in prod.)
- [x] Cross-tenant tests for every endpoint; inaccessible → 404. (Spec 007: `api/tests/db/test_rls.py`.)
- [ ] UUIDv7 identifiers; no sequential IDs exposed.

## Input handling
- [ ] Pydantic v2 strict models on every endpoint; size limits on bodies and uploads; pagination caps.
- [ ] Connector URLs validated against an allow-list and private-range rules (SSRF); DNS re-resolution pinned; redirects disabled.
- [ ] SQL only via SQLAlchemy parameters; DataFusion sessions read-only with timeouts; user SQL cannot reach Postgres.
- [ ] File uploads: type sniffing, size cap, stored outside web root, never executed.

## Transport and headers
- [ ] TLS 1.2+ at the edge; HSTS with preload; HTTP → HTTPS redirect.
- [ ] CSP: `default-src 'self'; script-src 'self' 'sha256-…' 'strict-dynamic'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'` (share embeds get their own policy); report-only first, then enforce.
- [ ] `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy` minimal, COOP `same-origin`.
- [ ] CORS disabled (same origin) except documented API-token clients.

## CSRF and XSS
- [x] Unsafe methods require the `X-Tabayyun-Request` header; `Origin` checked when present. (Spec 013: `CsrfMiddleware`. The item first also asked for a JSON content type; uploads are multipart, so the header, which a cross-site form cannot send, is the guard.)
- [ ] No inline scripts; no `dangerouslySetInnerHTML`; DOMPurify for markdown.

## Secrets and configuration
- [ ] No secrets in code, images or config files; environment variables or mounted secret files only; startup refuses placeholder secrets.
- [ ] Connector credentials encrypted at rest with a KMS-style envelope key (`TABAYYUN_MASTER_KEY` from secret store); write-only in the UI; rotation supported.
- [ ] Licence keys are Ed25519-signed; the public key is embedded; private key never leaves the vendor.

## Supply chain and build
- [ ] Lockfiles committed; `pnpm install --frozen-lockfile`, `uv sync --locked`, `cargo` with `Cargo.lock`.
- [ ] `cargo audit` + `cargo deny` (licences, advisories), `pip-audit`, `npm audit`/Socket in CI; Renovate with cooldown.
- [ ] SBOM (CycloneDX) for each image; images signed with cosign; Trivy scan gate.
- [ ] Reproducible builds for the Rust wheel; wheels built on manylinux with pinned toolchain.

## Multi-tenancy and data
- [ ] `org_id` on every tenant table and every cache path prefix; per-tenant rate limits; tenant-scoped job context.
- [ ] Backups: nightly Postgres dump + cache snapshot; restore drill documented and tested.
- [ ] Data retention configurable per workspace (findings, metrics, cache, audit).

## Logging, audit, monitoring
- [ ] Append-only `audit_events` for auth, authz, share, credential and threshold changes, and link views; export to SIEM via webhook/syslog.
- [ ] Structured logs without PII or secrets; request IDs propagated; OpenTelemetry traces.
- [ ] Alerts on auth failures spike, RLS policy errors, job failures.

## Process
- [ ] Threat model kept in `docs/architecture/threat-model.md` (STRIDE per component) and reviewed each release.
- [ ] Monthly dependency and base-image patch window; security release process documented; `SECURITY.md` with disclosure contact.
- [ ] Pen test before first paying customer; ASVS L2 self-assessment checked into repo.
