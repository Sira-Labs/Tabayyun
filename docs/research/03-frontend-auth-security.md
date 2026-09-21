# Research 03 — Frontend, auth, RBAC, admin, sharing and security stack

*Research date: 2026-09-21. Versions were checked against primary release pages where
possible; items that could not be verified are marked "(unverified)". Decisions derived from
this are in `docs/frontend/` and the ADRs.*

## 1. Recommended stack (summary)

| Area | Recommendation | Fallback / alternative |
|---|---|---|
| Frontend | **Vite 8 + React 19 SPA, TanStack Router + TanStack Query, shadcn/ui + Tailwind v4.3**, served as static files behind Caddy/nginx; FastAPI is the only server | TanStack Start (SSR, still RC) if SSR is ever needed; Next.js 16.3 only if hiring pool outweighs the security/ops cost |
| Admin panel | Same app, admin routes built with TanStack Table + shadcn; optionally **Refine v5 (headless)** for CRUD scaffolding | react-admin 5.x (MUI-bound, RBAC/audit in paid edition) |
| Identity (IdP) | **Keycloak 26.7** (Apache-2.0) or **Zitadel 4.17** (AGPL core, Apache SDKs); both give Google/OIDC/SAML brokering, organisations, passkeys, SCIM (preview) | Authentik (SCIM mature, weaker multi-tenant model); Ory Kratos/Hydra (build-your-own) |
| Session model | **Backend-for-frontend**: FastAPI does OIDC Authorization Code + PKCE against the IdP, stores session server-side, issues opaque `HttpOnly; Secure; SameSite=Lax` cookie. No tokens in the browser | — |
| Authorization | **App-level RBAC in Postgres** (memberships + shares tables) wrapped in one `authorize()` module, **Postgres RLS as safety net**. Upgrade path: Cerbos 0.55 (policy-as-code) or OpenFGA 1.21 / SpiceDB 1.56 (ReBAC) if sharing graphs get deep | Casbin (weak tooling), Oso library (deprecated), Permit.io (hosted) |
| Sharing | ACL rows for user/team shares; random 256-bit link tokens stored hashed with expiry/revocation/scope; signed-JWT embeds with locked parameters (Metabase pattern) | — |
| Charts | **uPlot 1.6.24** for time series (Grafana's renderer); **ECharts 6.1** for other chart types; **server-side MinMaxLTTB/M4 downsampling** via `tsdownsample` (Rust) or `lttb` crate in the Rust core | Perspective (FINOS) for pivot/streaming grids; lightweight-charts 5.2 (financial only) |
| Mobile | Responsive web + installable PWA now; **Capacitor 8.5** wrapper later if store presence / reliable push needed; Tauri 2 for desktop | Expo (only if UI rewritten in RN) |
| API | FastAPI 0.141 + OpenAPI → `@hey-api/openapi-ts` (or orval for TanStack Query hooks); **SSE** for live updates | WebSocket only for bidirectional; no tRPC (TS-only), no GraphQL (not needed) |
| i18n / a11y | Paraglide or Lingui (compile-time, ICU), WCAG 2.2 AA | react-i18next |

## 2. Framework comparison

Current versions: Next.js 16.3.3 (Active LTS) / 15.5.24 (Maintenance LTS) after the August
2026 security release; React Router 8 (2026-06-17, framework mode, middleware default, RSC
still unstable, needs Vite 7+); TanStack Start 1.x still labelled "Release Candidate" in its
own docs; SvelteKit 2.70 (SvelteKit 3 in RC); Nuxt 4.5 (Nuxt 3 EOL 2026-07-31); Vite 8
(Rolldown bundler, March 2026); Tailwind 4.3.2; shadcn/ui fully on Tailwind v4 + React 19.

| Framework | Pros | Cons for this product |
|---|---|---|
| Next.js 16 App Router | Largest ecosystem/hiring; mature RSC; monthly security-release cadence | Security track record: CVE-2025-29927 (middleware auth bypass via `x-middleware-subrequest`, CVSS 9.1), CVE-2025-55182 "React2Shell" (RSC deserialization RCE, CVSS 10, mass-exploited Dec 2025), July 2026 batch incl. another middleware/proxy bypass (CVE-2026-64642), SSRF in rewrites/Server Actions, Server-Action DoS. Self-hosters must patch monthly; Vercel-hosted users get WAF mitigations you won't. Needs a Node SSR server; caching model is complex; nonce-CSP needs custom server. |
| React Router 8 (framework mode) | Stable, self-host anywhere, loader/action model, middleware default | Needs Node SSR; RSC unstable; smaller ecosystem |
| TanStack Start | Best TS type-safety, Vite/Nitro deploy-anywhere, TanStack Query first-class, no Vercel coupling | Not yet 1.0 stable (RC); smaller community |
| **Vite + React SPA** | No server-side JS attack surface (no RSC, no middleware bypass class); static files behind nginx/Caddy; hash-based strict CSP feasible; simplest ops for self-hosting; fits "everything behind login, no SEO" | No SSR (irrelevant here); auth redirect/loading states handled client-side |
| SvelteKit 2 | Small bundles, great DX | Smaller B2B component ecosystem, fewer chart/admin libs, hiring |
| Nuxt 4 | Mature Vue full-stack | Vue ecosystem; same SSR ops burden |

**Why the SPA wins here:** the app is an authenticated dashboard; SSR brings no SEO value and
adds a second runtime to patch. Recent Next.js CVE history shows that "auth in middleware"
and RSC are recurring bypass/RCE classes; a static SPA + FastAPI keeps all authorization in
one place (the API). TanStack Router (type-safe, code-split routes) keeps a later move to
TanStack Start incremental.

**Admin:** Refine CORE v5 (Feb 2026: React 19, TanStack Query v5) is headless and works with
shadcn; react-admin 5.x is excellent but Material-UI-centric and its RBAC/audit-log features
are in the paid Enterprise edition. For a product admin panel (orgs, members, invitations,
audit log, feature flags) most teams build it with shadcn + TanStack Table; adopt Refine only
with many CRUD entities.

## 3. Authentication

| Option | Self-host | OIDC/SAML inbound (enterprise SSO) | SCIM | Passkeys | Orgs/multi-tenant | Notes |
|---|---|---|---|---|---|---|
| **Keycloak 26.7** (Apache-2.0) | Yes | Yes (broker per org, IdP mappers to org groups) | Preview SCIM API in 26.7 | Yes; discoverable-credential option in 26.7 | Organizations with fine-grained org admin roles | Heaviest (~600 MB idle in one benchmark); most complete |
| **Zitadel 4.17.3** (AGPL-3.0 core since v3, Apache SDKs) | Yes | Yes | SCIM 2.0 server at `/scim/v2/{orgId}` ("Preview") | Yes | Organizations + projects + delegated admin — best B2B model | Lightest footprint (~145 MB); AGPL needs legal review |
| Authentik | Yes | Yes | Yes (mature) | Yes | No true multi-tenancy | Great for internal apps; weaker for customer-facing B2B |
| Ory Kratos/Hydra | Yes | Yes | No | Yes | No | API-first, build your own UI; high effort |
| Better Auth 1.7 (TS) | Yes (Node) | `@better-auth/sso` (OIDC + SAML 2.0, per-org) | `@better-auth/scim` | Yes | Organization plugin | Now also maintains Auth.js. TypeScript-only — would force a Node auth service beside FastAPI |
| Auth.js/NextAuth v5 | Yes | OAuth only | No | No | No | "Now part of Better Auth"; security-patch mode; not for new projects |
| Lucia | — | — | — | — | — | Deprecated March 2025 |
| Clerk / Auth0 / WorkOS | No (hosted) | Yes | Yes | Yes | Yes | Contradict "self-hostable"; fine as optional cloud tier |
| SuperTokens | Yes | SAML needs BoxyHQ Jackson sidecar | via Jackson | Yes | Yes | Smaller protocol coverage |
| Logto | Yes | Yes | Partial | Yes | Yes | Young, breaking changes reported |
| Authelia | Yes | OIDC provider still "open beta" per own roadmap | No | Yes | No | Forward-auth gateway, not a CIAM |

**Recommendation:** run Keycloak (if Apache licensing matters) or Zitadel (lighter, more
B2B-native org model). Google login is a brokered IdP; enterprise customers get a
per-organisation OIDC/SAML connection (Entra ID, Okta, their own Keycloak); SCIM arrives via
the IdP's preview API. Treat SCIM as "preview" on both and test with Entra/Okta before
promising it.

**Session pattern (BFF):** FastAPI implements the OIDC client (Authorization Code + PKCE,
`state`/`nonce`), keeps IdP tokens server-side (Redis/Postgres), and sets an opaque session
cookie: `__Host-` prefix, `HttpOnly; Secure; SameSite=Lax; Path=/`. The browser never sees
JWTs. CSRF: SameSite=Lax blocks cross-site POST; additionally require a custom header on all
unsafe methods and reject non-JSON content types. Session rotation on login, absolute + idle
timeouts, server-side revocation, back-channel logout from IdP.

## 4. Authorization

| Tool | Model | Version / licence | Fit |
|---|---|---|---|
| Postgres tables + RLS | RBAC rows + DB safety net | — | **Start here.** ~2–4% overhead on indexed tenant columns; pitfalls: superuser/table-owner bypass, PgBouncer session state, per-table policies on joins, background jobs must set tenant context |
| Cerbos | Stateless policy-as-code (YAML/CEL), Python SDK, query-plan adapter for SQLAlchemy | 0.55.0 (2026-08-13), Apache-2.0 | Next step for attribute-heavy rules |
| OpenFGA | Zanzibar ReBAC, CNCF, Python SDK | 1.21.0 (2026-09-20), Apache-2.0 | When sharing inheritance becomes a graph problem |
| SpiceDB | Zanzibar ReBAC, strongest consistency (ZedTokens) | 1.56.x (unverified), Apache-2.0 | Same as OpenFGA; more ops weight |
| Casbin (pycasbin) | Embedded RBAC/ABAC | — | Works, but policy storage/tooling weaker; easy to misconfigure |
| Oso | Polar policy library | Library deprecated, Oso Cloud hosted | Avoid for new self-hosted |
| Permit.io | Hosted PDP | — | Not self-host-first |

**Pragmatic design:** `org_memberships(org_id, user_id, role)`,
`workspace_memberships(workspace_id, user_id|team_id, role)`,
`resource_shares(resource_type, resource_id, subject_type, subject_id, role, expires_at)`,
roles `owner > admin > editor > viewer`. One `authorize(user, action, resource)` function in
FastAPI computes effective role (max of org/workspace/direct/team share) and a list-filter
(`visible_resource_ids`) for queries; enable RLS on tenant tables with `SET LOCAL app.org_id`
per request so a forgotten filter cannot leak across orgs; write explicit cross-tenant
isolation tests. Move to Cerbos/OpenFGA only when rules outgrow SQL.

## 5. Sharing patterns

- **Grafana:** externally shared dashboards use a random access token in the URL; read-only;
  arbitrary queries impossible (only stored panel queries), variables disabled; email shares
  are one-time links valid 1 h, then a 30-day cookie; admins can pause/revoke; warns about
  query-load amplification.
- **Metabase:** public links (random string, no auth) vs signed embedding (JWT signed with
  shared secret, `exp`, "locked parameters" set server-side so viewers cannot widen filters).
- **Notion/Figma/Linear:** random unguessable URLs, optional expiry and password (Figma),
  guest accounts scoped to specific issues/teams (Linear), "duplicate as template" toggle
  (Notion). Cautionary example: ChatGPT-style share IDs derived from user/conversation IDs
  were enumerable.

**Rules:** (1) share token = 32 random bytes from a CSPRNG, store only its SHA-256 hash, never
derive from IDs; (2) token carries scope (resource id, fixed time range/params, view-only),
expiry, optional password, creator, revocation; (3) every access still goes through
`authorize()` with a synthetic "link viewer" principal; (4) all resource IDs are UUIDv7,
never sequential; (5) rate-limit and cache link endpoints, audit-log each view, list/revoke UI
per org, org policy to disable public links; (6) embeds: signed JWT with `exp` and locked
params, `frame-ancestors` restricted to customer domains.

## 6. Charts for large time series

| Library | Renderer | Version | 100k–1M points | Notes |
|---|---|---|---|---|
| **uPlot** | Canvas 2D | 1.6.24, ~48 KB | 166,650 pts in ~25 ms cold start; author says it "may begin to struggle beyond 100k in-view points" | Grafana's Time Series panel renderer; no animations by design |
| ECharts | Canvas/SVG | 6.1.0 (May 2026) | Progressive rendering, `sampling: 'lttb'`, 1M with `large` mode | ~1 MB, best breadth of chart types |
| Plotly.js | SVG/WebGL | 3.x | OK with scattergl, heavy bundle (~3.6 MB) | Exploratory, not dashboards |
| Observable Plot | SVG | — | Not for >50k marks | Pretty, not performant |
| Recharts / visx | SVG | — | Not suitable >10k | UI charts only |
| lightweight-charts | Canvas | 5.2.1 | Fast, financial/OHLC-oriented API | Skip unless candlesticks |
| Perspective (FINOS) | WASM | — | Streaming pivots | Tabular/pivot analysis pane |
| SciChart/LightningChart | WebGL | commercial | Millions of points | Paid; vendor benchmarks only |

**Approach (Grafana-style):** never ship raw points; the API downsamples to 2–4× pixel width
per series using MinMaxLTTB or M4 (M4 preserves min/max/first/last per pixel bucket, ideal
for anomaly-heavy quality data), then re-queries on zoom. `tsdownsample` (Rust + SIMD) or
the `lttb`/`minmaxlttb` crates fit the Rust core. Render with uPlot; keep ECharts for
bars/heatmaps/distributions.

## 7. Mobile

- **PWA:** installable on iOS (manual "Add to Home Screen"); Web Push works on iOS 16.4+ only
  when installed, no silent push/background sync; several 2026 sources report push
  unavailable in the EU on iOS 17.4+ (unverified against Apple docs).
- **Capacitor 8.5.2:** wraps the existing SPA for App Store/Play with reliable native push and
  biometrics; the standard "add app later" path.
- **Tauri 2:** mature desktop, mobile still maturing.
- **Expo SDK 55 / RN Web:** only if the UI is rewritten in React Native.
- Plan: responsive + PWA now (touch targets ≥24 px, container queries, offline shell),
  Capacitor later reusing the same build; keep auth on the BFF so cookies work in the WebView.

## 8. Security hardening checklist (OWASP ASVS 5.0 L2)

- **CSP:** hash-based strict CSP (`script-src 'sha256-…' 'strict-dynamic'`, hashes generated
  in CI), `object-src 'none'`, `base-uri 'none'`, `frame-ancestors` allow-list; consider
  `require-trusted-types-for 'script'` (Trusted Types reached Baseline in 2026, reported);
  test chart libs first.
- **XSS:** no `dangerouslySetInnerHTML`; DOMPurify for user markdown; no CDN scripts.
- **Cookies/CSRF:** `__Host-` prefixed, `HttpOnly; Secure; SameSite=Lax`; custom-header CSRF
  check on unsafe methods; `Origin` validation; never `SameSite=None`.
- **Headers:** HSTS preload, `X-Content-Type-Options`, `Referrer-Policy:
  strict-origin-when-cross-origin`, `Permissions-Policy`, COOP/COEP where feasible.
- **Auth:** OIDC code+PKCE only; passkeys via IdP; MFA enforced per org; session
  rotation/revocation; password rules per NIST 800-63.
- **AuthZ:** single `authorize()` path; RLS safety net; deny-by-default; cross-tenant tests
  in CI; never auth in "middleware" only.
- **API:** rate limiting (per IP, session, org), request size limits, pagination caps, SSRF
  allow-list for customer-provided URLs (connectors!), strict pydantic validation.
- **Secrets:** env/secret files via Docker/K8s secrets or SOPS/age; never in images.
- **Supply chain:** 2025–26 saw Shai-Hulud, chalk/debug, Axios (Mar 2026) compromises. Pin +
  commit lockfiles, `pnpm install --frozen-lockfile`, pnpm `minimumReleaseAge` cooldown,
  disable install scripts by default, Socket/Snyk + `npm audit`, `cargo audit`/`cargo deny`,
  `pip-audit`, Renovate, SBOM (CycloneDX), signed images (cosign), Trivy.
- **Audit log & SOC 2 readiness:** append-only audit table (actor, org, action, target, IP,
  UA, before/after), admin/authz changes and share-link views logged, retention policy,
  export; structured logs without PII; backup/restore drills.
- **Multi-tenant isolation:** org_id on every table, RLS, tenant-scoped cache keys and jobs,
  per-tenant rate limits, storage prefixes.

## 9. API layer and real-time

- **REST + OpenAPI** from FastAPI with generated TS client: `@hey-api/openapi-ts` or orval
  (TanStack Query hooks). tRPC is TS-only; GraphQL adds attack surface without benefit.
- **SSE** for dashboard/alert updates (`sse-starlette`); WebSockets only for collaborative
  editing.

## 10. i18n and accessibility

- i18n: Paraglide (compiler, typed message functions) or Lingui (ICU); `Intl` for
  dates/numbers/units; RTL-ready layout via CSS logical properties.
- WCAG 2.2 AA: Focus Not Obscured, Target Size ≥24×24 px, dragging alternatives (chart
  brushing needs input fallbacks), Accessible Authentication. shadcn/Radix primitives give a
  keyboard/ARIA baseline; add chart data tables/exports as text alternatives; test with axe.

## 11. Uncertainties

- TanStack Start is still RC per its own docs.
- SCIM on both Keycloak (26.7) and Zitadel is labelled preview.
- iOS EU push restriction and Trusted Types Baseline status come from secondary articles.
- SpiceDB exact latest tag is from a search snippet.
- Chart benchmarks beyond uPlot's own README are vendor-authored.

## Sources (verified)

- Next.js: https://nextjs.org/blog/july-2026-security-release · https://nvd.nist.gov/vuln/detail/cve-2025-29927 · https://www.wiz.io/blog/critical-vulnerability-in-react-cve-2025-55182 · https://cloud.google.com/blog/topics/threat-intelligence/threat-actors-exploit-react2shell-cve-2025-55182
- Frameworks: https://tanstack.com/start/latest/docs/framework/react/overview · https://www.infoq.com/news/2026/08/react-route-v8/ · https://vite.dev/blog/announcing-vite8 · https://ui.shadcn.com/docs/tailwind-v4 · https://refine.dev/blog/react-admin-vs-refine/ · https://marmelab.com/blog/2026/02/26/react-admin-february-2026-update.html
- Auth: https://www.keycloak.org/2026/07/keycloak-2670-released · https://github.com/zitadel/zitadel/releases · https://zitadel.com/docs/apis/scim2 · https://www.better-auth.com/docs/plugins/sso · https://authjs.dev/ · https://github.com/lucia-auth/lucia · https://www.authelia.com/roadmap/active/openid-connect-1.0-provider/
- AuthZ: https://github.com/cerbos/cerbos/releases · https://github.com/openfga/openfga/releases · https://github.com/authzed/spicedb/releases · https://www.osohq.com/docs/oss/any/getting-started/deprecation.html · https://queryplane.com/blog/postgres-row-level-security-in-practice/ · https://www.cerbos.dev/ecosystem/fastapi
- Sharing: https://grafana.com/docs/grafana/latest/visualizations/dashboards/share-dashboards-panels/shared-dashboards/ · https://www.metabase.com/docs/latest/embedding/static-embedding · https://www.metabase.com/docs/latest/embedding/securing-embeds · https://help.figma.com/hc/en-us/articles/16142157359255
- Charts: https://github.com/leeoniya/uPlot · https://github.com/apache/echarts/releases · https://github.com/finos/perspective · https://arxiv.org/abs/2307.05389 (tsdownsample) · https://lib.rs/crates/lttb
- Mobile: https://www.magicbell.com/blog/pwa-ios-limitations-safari-support-complete-guide · https://ionic.io/blog/capacitor-8-5-released
- Security: https://github.com/OWASP/ASVS · https://web.dev/articles/strict-csp · https://unit42.paloaltonetworks.com/npm-supply-chain-attack/ · https://www.pkgpulse.com/guides/npm-supply-chain-security-guide-2026
- API/i18n/a11y: https://fastapi.tiangolo.com/advanced/generate-clients/ · https://orval.dev/ · https://websocket.org/comparisons/sse/ · https://paraglidejs.com/react-i18next-alternatives · https://www.w3.org/TR/WCAG22/
