# Spec 005 — Runs list and run report in the web app

Sprint 6, story S6-5 and S5-4. Depends on: 002, 003, 004. Packages: `web/src/` (routes,
API client, components), `web/src/types.ts`.

## Goal

The web app shows persisted runs. Uploading starts a run and polls it; the run report shows
the score tiles, the findings with expandable evidence and the skipped checks; the list shows
recent runs. Refreshing the browser loses nothing.

## User story

As a data engineer, I upload a file, watch the run go from queued to succeeded, open the
report, expand a finding to read its evidence, and find the same report tomorrow in the list.

## Interface

Routes (TanStack Router):

```
/                 redirects to /runs
/runs             list: status badge, series external id, started, duration, findings, score
/runs/new         upload form (existing fields + physical_min, physical_max), POST /api/runs
/runs/:id         report: header (status, timing, stats), score tiles per dimension,
                  findings table (check, severity, window, summary; row expands to evidence
                  JSON rendered as key/value), skipped checks, link to the series
```

API client (`web/src/api.ts`): `createRun(form)`, `getRun(id)`, `listRuns(cursor)`,
`listFindings({run_id, cursor})`. Polling: TanStack Query `refetchInterval` 2 s while
`status` is `queued` or `running`, off when terminal.

## Behaviour

1. Submitting the form posts to `/api/runs` and navigates to `/runs/:id` on 202; a 4xx shows
   the server's `detail` next to the form and keeps the input.
2. `/runs/:id` renders immediately with the queued/running state and a progress indicator,
   then the full report when terminal; a `failed` run shows the error message and a "try
   again" link back to `/runs/new`.
3. Findings load through `/api/findings?run_id=…&status=all`, sorted by severity then window
   start; the evidence panel renders numbers with `format.ts`, timestamps in the browser's
   zone with the UTC offset shown, and nested objects as indented key/value lists, never raw
   JSON strings.
4. The list paginates with "load more" using `next_cursor`; each row links to the report.
5. Keyboard: every expandable row is a button with `aria-expanded`; status changes are
   announced through an `aria-live="polite"` region; the CSRF header from sprint 2 is sent on
   every request.
6. Mobile: the findings table collapses to stacked cards under 640 px; score tiles wrap.

## Acceptance criteria

- [ ] Upload → report round trip works against the deployed API with a worker, and with
      `TABAYYUN_INLINE_JOBS=true` locally. (Local half verified 2026-09-23 in Chromium: upload, report,
      reload, failed run, no console errors. The deployed half is checked after merge.)
- [x] Reloading `/runs/:id` shows the same report from persisted data.
- [x] A failed run shows its error text; the console shows no unhandled promise rejection.
- [x] `physical_min`/`physical_max` entered in the form appear in the run's series metadata.
- [x] `pnpm lint`, `pnpm build` and `pnpm test` pass; the vitest suite covers the polling
      hook, evidence rendering and the form's error path.
- [x] Lighthouse accessibility score ≥ 95 on `/runs/:id`.
- [x] Screenshots of `/runs`, `/runs/new` and `/runs/:id` at desktop and phone width are in
      the PR.

## Test cases

Vitest (`web/src/__tests__/`): `useRunPolling.test.tsx` (stops when terminal),
`Evidence.test.tsx` (nested objects, timestamps, numbers), `RunForm.test.tsx` (422 detail
shown, input kept), `RunsList.test.tsx` (load more appends).

Manual: the two browsers the project supports (Chromium, Safari) at 375 px and 1280 px.

## Implementation edits

- The run report lists skipped checks from a new `stats.skipped_checks` (the run kept only
  their count); older runs show none.
- "Link to the series" is a series panel (name, external id, unit, physical limits, open
  findings, runs) from `GET /api/series/{id}`: the series page arrives in sprint 9. The panel
  also shows that form limits reached the series metadata.
- Score tiles come from the run's row in `GET /api/series/{id}/scores?run_id=` (a new
  optional filter on that endpoint, so the row is found however many runs followed); the run
  itself carries only the overall score, used when the row is missing.
- The report follows `next_cursor` over the findings list (up to 50 pages of 500), so a run
  with more findings than one page shows them all.
- Error bodies that are not JSON (a proxy's plain-text answer) are shown as text; HTML error
  pages fall back to the status line.
- The runs list hides Duration and Findings below 640 px; Status, Series, Started and Score
  stay.
- The form gains the epoch timestamp unit select (spec 004, ADR-0014).
- The findings list is one `<ul>` whose rows are buttons laid out as a grid from 640 px and
  stacked below, instead of a table plus a separate card list; one DOM for both widths.
- Known limitation: a run's findings are those it created or last updated
  (`run_id` filter, spec 003). A finding a later run merged into drops out of an older run's
  report until findings keep a per-run link.
- Lighthouse accessibility: 100 on `/runs/:id`, `/runs` and `/runs/new` (Chromium 1194,
  desktop). Screenshots are in `docs/assets/screenshots/`.
- Vitest runs with `TZ=Asia/Riyadh` so the offset suffix is covered; jsdom ignores files set
  by user-event for `required`, so the form tests submit the form directly. Extra suites
  `RunReport.test.tsx` covers sorting, expandable evidence, skipped checks, the series panel
  and the failed state; `api.test.ts` covers error text and paging.

## Out of scope

- Series detail page with chart (sprint 9).
- Findings inbox with triage actions (sprint 10); this page shows findings read-only.
- Login (sprint 8).
