# Spec 024 — Sources pages and series catalogue

Sprint 9, story S9-4. Depends on: 004 (series API), 014 (workspace roles), 021–023 (connector
sources, jobs and their routes). Packages: `web/` (pages, router, API client), `api/` (two
additions to the series API).

## Goal

The web app gets its first screens for connectors and series:

- **`/sources`** lists the workspace's sources with their health and last fetch.
- **`/sources/new`** creates a PI Web API, OPC UA or synthetic source (admins).
- **`/sources/:id`** shows a source:
  - its health, fetch history and config;
  - the actions an editor may take: check, fetch a window, find and add points, and import
    metadata, each following its job until it ends;
  - what an admin may change: credentials (write-only), enabled, and config;
  - for OPC UA, the client certificate and a helper to pin the server certificate.
- **`/series`** is the catalogue: search, filters (source, unit, kind, "score below"), and
  paging. The filters live in the URL.

## User story

As a workspace admin, I add the plant historian, confirm its certificate and see it healthy,
without curl. As an engineer, I find tags, add them, and later find the worst series of a unit
or source in the catalogue.

## Interface

### API additions

| Route | Change |
|---|---|
| `GET /api/series` | new query `unit` (exact, ignoring case) and `score_max` (0–100: the latest raw overall score is at or below it; series without a score are left out) |
| `GET /api/series/units` | `{"items": [{"unit": "m3/h", "n": 12}]}`: the workspace's distinct non-empty units, grouped ignoring case as the filter matches them, by count, then unit |
| `GET /api/sources` | each item gains `last_fetch` (from its health), so the list needs no request per source |

### Web routes

| Route | Who | Content |
|---|---|---|
| `/sources` | viewer | table: name (link), type, health, enabled, series, last fetch; "New source" for admins |
| `/sources/new` | admin | type, name, and the type's config form; on success, the new source's page |
| `/sources/$sourceId` | viewer | header and health; actions (editor); credentials and settings (admin); OPC UA certificate; fetch history |
| `/series` | viewer | search box, filters (source, unit, kind, score below 80, 60 or 40), table, "Load more" |

- The URL search of `/series` is `q`, `source`, `unit`, `kind` and `score`.
- Navigation gains "Series" and "Sources".

### Config forms

| Type | Fields (spec) |
|---|---|
| `pi_web_api` | base URL, data server, AF database, CA certificates (PEM), poll interval (022) |
| `opc_ua` | endpoint URL, security policy, security mode, allow insecure, server certificate SHA-256, browse root, poll interval (023) |
| `synthetic` | the whole config as JSON (021) |

- Editing keeps the config keys the form does not show, such as limits and settle time.
- Empty optional fields are left out.
- The server's `invalid_config` field errors are shown next to the form.

### Credentials forms

| Type | Fields |
|---|---|
| `pi_web_api` | Basic (username, password) or bearer (token) |
| `opc_ua` | anonymous or username (username, password); optionally the client certificate and key as PEM |

- The fields are never filled from the server.
- The section shows only whether credentials are set, and since when.
- Admins can clear them.

## Behaviour

1. **Roles.** The current workspace's role decides what shows:
   - viewers see everything read-only;
   - editors also see the actions;
   - admins also see credentials, settings and "New source".
   - An org owner or admin counts as an admin, as on the server.
2. **Jobs.** An action posts its job and shows "queued" or "running" until the job ends, polling
   `GET …/fetches/{id}` every 1.5 s. Then it shows the outcome:
   - a check: the status and error;
   - a fetch: the rows, and point errors;
   - a search: the matches, with check boxes and an "Add as series" button;
   - a metadata import: updated, unchanged and failed counts, with each series' changes or
     error.

   The source's health and history refresh when the job ends.
3. **Finding and adding points.** The editor types a query and gets the matches. They tick the
   points to add, which registers them as series with their names and units, and see how many
   were created and how many already existed.
4. **Pinning (OPC UA).** When the source's last error says the server certificate is not
   pinned, the page shows the thumbprint and subject with a "Pin this certificate" button for
   admins. It saves the config with `server_certificate_sha256` set, after a confirmation that
   repeats the thumbprint.
5. **Client certificate (OPC UA).** Once credentials are set, the page shows the SHA-1 and
   SHA-256 thumbprints, the application URI and expiry, and a download of the PEM file.
6. **Catalogue.**
   - Changing a filter replaces the URL search and starts again from the first page; the
     search box applies on submit.
   - Rows show the name, external id, source name, unit, kind, latest score, open findings and
     last run.
   - "Load more" follows `next_cursor`.

## Acceptance criteria

- [x] `GET /api/series?unit=&score_max=` filter as specified, and `GET /api/series/units`
  counts units, both within the workspace.
- [x] `/sources` lists sources with health and last fetch; "New source" only for admins.
- [x] `/sources/new` creates PI Web API, OPC UA and synthetic sources and shows field errors.
- [x] `/sources/$sourceId`:
  - actions only for editors and admins;
  - credentials and settings only for admins;
  - viewers see no action buttons.
- [x] Check, fetch, search and metadata jobs are followed to their end and their results
  shown. Search results can be added as series.
- [x] Credentials forms send the right shapes, are never filled from the server, and can be
  cleared.
- [x] The OPC UA pin helper saves the thumbprint after confirmation. The client certificate is
  shown and can be downloaded.
- [x] `/series` filters by text, source, unit, kind and score below; the filters are in the
  URL; "Load more" pages on.
- [x] `pnpm lint`, `pnpm build` and `pnpm test` pass; the API tests cover the additions.

## Test cases

Web (`web/src/__tests__`):

- `SourcesList.test.tsx`: the table, health and last fetch, "New source" by role.
- `SourceNew.test.tsx`: PI and OPC UA forms post the right config; `invalid_config` errors are
  shown; synthetic JSON.
- `SourceDetail.test.tsx`:
  - viewer, editor and admin sections;
  - a check followed to its end;
  - search, then add;
  - metadata result;
  - credentials payloads and clearing;
  - pinning and the client certificate.
- `SeriesCatalogue.test.tsx`: filters to URL and request; units and sources in the menus;
  "Load more".

API (`api/tests/db/test_series_api.py`): `unit` and `score_max` filters; units counts per
workspace.

## Implementation notes

- **Re-validating the search.** TanStack Router merges the root route's raw search into each
  child's, so `/series` validates its filters again when it reads them; unknown keys and values
  never reach the API.
- **Roles** come from `/api/workspaces`, which already gives each workspace's effective role.
  An org owner or admin also counts as an admin, as on the server.
- **Jobs** are followed by polling every 1.5 s, until server-sent events arrive in S10-4. The
  history refreshes when a job ends.
- **Spec edits made during implementation:** `GET /api/sources` gained `last_fetch`.

## Out of scope

- The series detail page with its chart (S9-5); catalogue rows link nowhere until then.
- A virtualised table for very large catalogues.
- Bulk actions in the catalogue.
- Deleting a source (retention, S13).
- Live updates by server-sent events (S10-4); jobs are polled.
