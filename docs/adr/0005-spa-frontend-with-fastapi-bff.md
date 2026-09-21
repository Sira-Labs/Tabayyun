# ADR-0005: Vite + React SPA served statically, FastAPI as backend-for-frontend

- **Status:** Accepted
- **Date:** 2026-09-21

## Context
The UI is an authenticated dashboard with no SEO needs. It must be self-hostable, work on
mobile and desktop, and minimise the attack surface. Next.js has had repeated
middleware-auth-bypass and RSC vulnerabilities in 2025–2026 that primarily hit self-hosters.

## Decision
Vite 8 + React 19 SPA with TanStack Router and TanStack Query, shadcn/ui + Tailwind v4,
built to static files served by Caddy/nginx with a hash-based strict CSP. FastAPI is the only
server: it performs the OIDC code+PKCE flow, keeps tokens server-side and sets an opaque
`__Host-` session cookie. Typed API client generated from OpenAPI (orval / hey-api). Live
updates via SSE. PWA manifest and service worker for installability.

## Alternatives considered
| Option | Pros | Cons | Why not |
|---|---|---|---|
| Next.js App Router | Ecosystem, hiring | Node SSR runtime to patch monthly, CVE history, complex caching | Attack surface and ops cost |
| TanStack Start / React Router 8 | Type-safe SSR | Still RC / needs Node runtime | No SSR benefit here; can migrate later |
| SvelteKit | Small bundles | Smaller B2B component and chart ecosystem | Hiring and ecosystem |
| Refine / react-admin for the whole app | CRUD speed | Opinionated, MUI-bound, paid RBAC | Use shadcn + TanStack Table; Refine optional for admin |

## Consequences
No server-rendered pages; first paint shows a shell until session check completes. All
authorization lives in the API.
