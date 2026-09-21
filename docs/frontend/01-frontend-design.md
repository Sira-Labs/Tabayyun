# Frontend design

Stack decisions: ADR-0005 (SPA + BFF), ADR-0006 (IdP), ADR-0007 (RBAC), ADR-0008 (charts).
Research: `docs/research/03-frontend-auth-security.md`.

## Stack

| Layer | Choice | Notes |
|---|---|---|
| Build | Vite 8, TypeScript strict, pnpm with frozen lockfile and `minimumReleaseAge` | No install scripts by default |
| UI | React 19, shadcn/ui (Radix primitives), Tailwind v4 | Design tokens in CSS variables; light/dark |
| Routing / data | TanStack Router (type-safe, file-based, code-split) + TanStack Query | Query keys mirror REST resources |
| Forms / validation | react-hook-form + zod schemas generated from OpenAPI | Same schemas as API |
| Tables | TanStack Table (virtualised) | 40k-series catalogue must scroll smoothly |
| Charts | uPlot (time series), ECharts (bars, heatmaps, distributions) | Server downsampled |
| API client | orval → typed client + TanStack Query hooks from FastAPI's OpenAPI | Generated in CI, committed |
| Live | SSE (`EventSource`) for run/finding events | Reconnect with backoff |
| i18n | Lingui (ICU messages, compile-time) — English first, German second | `Intl` for dates, numbers, units |
| PWA | manifest + Workbox service worker (app shell only, no data caching) | Installable on iOS/Android |
| Testing | Vitest + Testing Library, Playwright e2e (incl. mobile viewports), axe a11y checks | |

## Information architecture

```
/login                               IdP redirect; org picker if user belongs to several
/w/:workspace
  /overview                          workspace score, worst series, open findings, run health
  /series                            catalogue: search, filters (source, unit, score, kind), bulk actions
  /series/:id                        series detail: chart + findings overlay, profile, checks, history
  /findings                          triage inbox: group by check / series / asset; ack, mute, resolve, assign
  /datasets                          datasets and their series selection
  /suites, /suites/:id               check suites, schedules, per-check thresholds, run history
  /runs/:id                          run report
  /sources, /sources/:id             connectors, health, credentials (write-only fields), metadata import
  /alerts                            rules and channels
  /reports                           scheduled quality reports (PDF/CSV export), shareable
  /settings                          workspace settings, members, teams, shares, API tokens
/admin                               org admin: workspaces, members, SSO, audit log, licence, plugins
/share/:token                        link viewer (read-only, locked scope)
/me                                  profile, sessions, passkeys (delegated to IdP account console)
```

## Key screens

### Overview
- Score tiles per dimension with 30-day sparkline; "worst 5% series" list next to the mean.
- Findings by severity over time (stacked bar), top checks firing, connector health strip.
- Everything drills down with filters preserved in the URL (shareable state).

### Series detail
- uPlot chart with findings as shaded windows, quality flags as a rug strip below, baseline
  band toggle. Brush to zoom → re-query; keyboard alternative (range inputs) for WCAG 2.2.
- Right panel: profile (interval, resolution, unique values, stats, seasonality), metadata
  (unit, limits, asset path) editable by editors, related series.
- Tabs: Findings, Checks (effective thresholds, with "why this threshold" explanation:
  metadata / auto-baseline / override), Score history, Audit.

### Findings inbox
- Virtualised table, saved filters, group-by. Row expands to evidence: statistic, threshold,
  window, small chart, explanation text from the check's `explain` template.
- Bulk actions: acknowledge, mute (with expiry and reason), resolve, assign, export CSV,
  "create ticket" webhook.
- Feedback loop: "false positive" marks feed the threshold suggestion job.

### Admin panel
- Built in-app with shadcn + TanStack Table (Refine optional later).
- Organisations, workspaces, members and roles, teams, invitations, SSO connection per
  org, audit log viewer with filters and export, licence and entitlements, plugin registry,
  share link inventory with revoke.

### Sharing UI
- "Share" button on series views, findings views, dashboards and reports.
- Tabs: People (user/team + role + expiry), Link (create, scope preview, expiry, optional
  password, copy, revoke), Embed (later).
- Link viewer page shows the locked scope banner and no navigation.

## Responsive and mobile

- Mobile-first layout with container queries; nav collapses to a bottom bar (Overview,
  Findings, Series, Alerts, More).
- Touch targets ≥ 24×24 px; charts support pinch-zoom via uPlot touch plugin.
- Tables switch to card lists below 640 px.
- PWA installable; Web Push for alerts where the platform supports it; Capacitor wrapper
  later for store presence (same build, cookie auth works in the WebView with correct
  `server.hostname`).

## Security in the frontend

- No tokens in JavaScript; session cookie only. All unsafe requests send
  `X-Tabayyun-Request: 1` (CSRF check) and JSON bodies.
- Strict hash-based CSP generated at build; no inline scripts; no third-party CDN.
- No `dangerouslySetInnerHTML`; markdown in comments sanitised with DOMPurify.
- Route guards are UX only; the API is the authority. 404 for inaccessible resources.
- Dependency policy: Renovate, Socket/npm audit in CI, lockfile committed, cooldown on new
  package versions.

## Accessibility

WCAG 2.2 AA target: keyboard reachability for every action, visible focus, colour is never
the only signal (severity uses icon + text), chart data available as table/CSV, ARIA live
regions for SSE updates, reduced-motion respected.
