# Spec 026 — File-drop source (`csv_dir`)

Sprint 9, story S9-6. Depends on: 021 (connector framework), 006 (Parquet cache and coverage),
024 (sources pages). Packages: `api/` (connector, ingest job, sources API, migration 0010),
`web/` (source form and page), `deploy/`, `docs/`.

## Goal

A source of type `csv_dir` ingests CSV and Parquet files that appear in a folder on the server,
for sites that cannot be reached over the network (a weekly USB copy, a one-way data diode, an
export job writing to a share). Every file becomes rows of one or many series in the raw layer
of the cache. Series the files name are created on the fly. Each file is recorded once, with
its rows, series, time span or error, so a rescan skips what it already read, and a changed
file is read again.

Connectors so far fetch a requested window, and the cache records which windows it holds. A
folder cannot answer "give me last Tuesday": files arrive late and cover any period. If the
fetch engine handled this source, it would mark windows as covered before their data arrived,
and they would never be filled. So this source does not read windows. It ingests whatever
files appear, in a job of its own.

## User story

As the data engineer of an air-gapped site, I drop exports into a folder that the Tabayyun
server can read. Their series appear in the workspace and are checked like any other, without
anyone opening a network path into the plant.

## Interface

### Server setting

| Variable | Default | Meaning |
|---|---|---|
| `TABAYYUN_FILE_SOURCES_ROOT` | unset | Folder under which `csv_dir` sources read. Unset: their checks and scans fail with `file sources are not enabled on this server (TABAYYUN_FILE_SOURCES_ROOT)`. |

The worker needs it, and the api too when it runs jobs inline (`TABAYYUN_INLINE_JOBS`).

### Connector framework additions (`tabayyun.connectors`)

- `Connector.reads_windows: ClassVar[bool] = True`. A connector with `False` is not asked for
  windows: manual fetches are refused, dataset runs do not fetch for it, and polling queues an
  ingest instead of a fetch.
- `connectors.build(type, config, credentials, net, *, files_root=None)`. The instance gets
  `files_root: Path | None`, the resolved `TABAYYUN_FILE_SOURCES_ROOT`.
- `Connector.check()` may return a dict; the check job stores it as its `result`.

### `csv_dir` config

`ConnectorConfig` keys of spec 021 (`poll_interval_s`, `settle_s` with default 60 here; the
window keys have no effect), plus:

| Key | Default | Meaning |
|---|---|---|
| `path` | required | Folder relative to the root, 1–500 characters. `.` is the root itself. Absolute paths, `..`, backslashes and control characters are refused. |
| `recursive` | `false` | Also read subfolders. |
| `pattern` | `*` | File name pattern (`fnmatch`), on top of the extensions `.csv` and `.parquet`. |
| `layout` | `long` | `long`: a column names the series. `wide`: every column except the timestamp is a series. |
| `ts_column` | `ts` | Timestamp column: RFC 3339 text, or epoch integers. |
| `ts_unit` | `auto` | `auto`, `s`, `ms`, `us` or `ns` for epoch integers (ADR-0014). |
| `series_column` | `series` | Long layout: the series id. |
| `value_column` | `value` | Long layout: the value. |
| `quality_column` | none | Long layout, optional: quality text as uploads read it. Without it every sample is good. |
| `delimiter` | `,` | CSV field separator, one character. |
| `max_file_mb` | 256 | Larger files are skipped; 1–4096. |
| `max_files_per_scan` | 100 | Files one scan reads at most; the rest wait; 1–1000. |
| `max_series_per_file` | 1000 | A file naming more series fails; 1–10000. |

### Database (migration 0010)

- `source_fetches.trigger` may be `ingest`.
- `source_files`, row-level security by `org_id` like `source_fetches`, one row per path of a
  source:

| Column | Type | Meaning |
|---|---|---|
| `id` | uuid | key |
| `org_id`, `workspace_id` | uuid | tenant |
| `source_id` | uuid | FK `sources`, cascade |
| `path` | text | relative to the source's folder, `/`-separated; unique with `source_id` |
| `size` | bigint | bytes when last read |
| `mtime` | timestamptz | modification time when last read |
| `sha256` | text | content hash when last read |
| `status` | text | `ingested` or `failed` |
| `rows` | bigint | rows written |
| `n_series` | int | series in the file |
| `n_new_series` | int | series it created |
| `start_at`, `end_at` | timestamptz | time span of its rows, null when it has none |
| `error` | text | why it failed |
| `versions` | int | how often a changed version was read |
| `fetch_id` | uuid | the scan that last read it; FK `source_fetches`, set null (scans are pruned after 30 days, files are not) |
| `created_at`, `updated_at` | timestamptz | first and last read |

Index `(source_id, updated_at DESC, id DESC)`.

### API

| Route | Who | Response |
|---|---|---|
| `POST /api/sources/{id}/ingest` | editor | 202 `{id}` of a queued `ingest` job ("Scan now") |
| `GET /api/sources/{id}/files?status=&limit=&cursor=` | viewer | `{items: [{id, path, size, mtime, status, rows, n_series, n_new_series, start_at, end_at, error, versions, fetch_id, updated_at}], next_cursor}`, last read first; `limit` 1–200, default 50 |

- `POST …/ingest` on a source that reads windows is 409 `not_supported`; on a disabled source
  409 `source_disabled`.
- `POST …/fetches` on a `csv_dir` source is 409 `not_fetchable`. Search and metadata are 409
  `not_supported`, as for any connector without them.

### Web

- **New source:** the `csv_dir` type has typed fields for the config above, with a layout select
  that shows the long-layout columns only for `long`.
- **Source page of a `csv_dir` source:** Check and **Scan now** instead of Fetch, Search and
  Metadata. A **Files** panel lists the files, last read first, with status, rows, series, span
  and error, filterable by status, with "Load more". The job history shows each scan's counts.

## Behaviour

1. **Saving a source.** The config validates as above. A long layout needs `series_column` and
   `value_column`, distinct from `ts_column`.
2. **Check** (job `check`). It resolves the folder inside the root, and fails when the root is
   unset, the folder is missing, not a folder, outside the root or unreadable. On success the
   job's `result` is `{files, bytes}`: the matching files the folder holds now.
3. **Polling.** A due `csv_dir` source (spec 021's `poll_interval_s`) gets an `ingest` job
   instead of a fetch. Manual fetches are refused (409 `not_fetchable`). Dataset runs skip it
   when they fill gaps, and note its members' missing data as for any source.
4. **Scan** (job `ingest`, on the fetch queue under the source's lock, not retried):
   1. **Find.** It lists the folder (and its subfolders with `recursive`) for regular files
      named `*.csv` or `*.parquet` that match `pattern`. It skips hidden files and folders
      (a name starting with `.`), temporary files (`~$*`, `*~`, `*.tmp`, `*.part`, `*.partial`,
      `*.crdownload`), and symbolic links that resolve outside the root. It also skips files
      changed within `settle_s` (still being copied), and files larger than `max_file_mb`. It
      lists at most 100 000 entries.
   2. **Skip known files.** A file whose path has a row with the same size and modification
      time is known. Otherwise its SHA-256 is computed, and a row with the same hash is known
      too (the row takes the new time). A known file is skipped, whether it was ingested or
      failed: the same content would fail the same way.
   3. **Order and cap.** Unknown files are read oldest first (by modification time, then
      path), at most `max_files_per_scan`; the result says when more wait.
   4. **Read.** The file becomes rows of (series id, time, value, quality):
      - Timestamps follow the upload rules (RFC 3339 text or epoch integers, `ts_unit`). A row
        without a timestamp fails the file.
      - Values are numbers; empty cells and `NA`, `NaN`, `null` are NaN.
      - Long layout: series ids are text, trimmed; an empty id or one longer than 256
        characters fails the file.
      - Wide layout: each column other than the timestamp is a series named by its header. A
        column that is not numeric fails the file.
      - A missing column fails the file and names it.
   5. **Create series.** Ids the source does not have yet become series of it (name = id, kind
      `measurement`). A file naming more than `max_series_per_file` series fails before
      anything is created.
   6. **Store.** Each series' rows go to the raw layer of the cache, and coverage is recorded
      for the span of its rows. A later write of the same instant wins, so a changed file's
      values replace the old ones.
   7. **Record.** The file's row is inserted or updated with its outcome in one transaction
      with its series and coverage. A re-read changed file adds one to `versions`.
   8. **Isolation.** A file that fails gets its error recorded and the scan goes on. An error
      outside any file (the root unset, the folder gone, the cache unwritable) ends the scan.
5. **Outcome.** The scan's `rows` are the rows written; its `result` is
   `{files: [{path, status, rows, series, new_series, error}], known, settling, too_large: [path],
   outside_root: [path], more}`. It is `succeeded` when no file failed, `partial` with
   `N file(s) failed: <first error>` when some did, and `failed` when the scan stopped. The
   source's health follows as for fetches (spec 021).
6. **Errors** shown to users name the file and the column, never a server path above the root.
   Anything unexpected while reading a file is recorded as `could not read the file` and logged
   with its type.
7. **Files are only read.** They are never moved, changed or deleted, so the folder can be
   mounted read-only.

## Acceptance criteria

- [ ] `csv_dir` sources save with a validated config; absolute paths and `..` are refused.
- [ ] Check confirms the folder inside the root and counts its files; without the root setting
  it fails with the "not enabled" message.
- [ ] A scan ingests long and wide CSV and Parquet files: series appear, the cache holds their
  rows, coverage spans them, and `source_files` records each file.
- [ ] A rescan skips known files; a changed file is read again and its values win; a failed
  file does not stop the others; settling, too large, hidden and outside-root files are
  skipped.
- [ ] Polling queues an ingest for a due `csv_dir` source; manual fetches are 409
  `not_fetchable`; dataset runs do not fetch for it.
- [ ] `POST /api/sources/{id}/ingest` and `GET /api/sources/{id}/files` work with their roles
  and keep to the caller's workspace (RLS route list).
- [ ] The web form creates a `csv_dir` source; its page shows Check, Scan now and Files, and no
  Fetch, Search or Metadata.
- [ ] Compose and CapRover docs mount a read-only drop folder and set the root.
- [ ] `make lint` and `make test` (or the relevant subsets) pass.

## Test cases

- **Unit (`api/tests/test_csv_dir.py`):**
  - config: path rules, long-layout columns, limits;
  - reading: long and wide CSV, Parquet, epoch units, quality text, a `;` delimiter, missing
    columns, bad timestamps, empty and long series ids, non-numeric wide columns;
  - listing: pattern, hidden and temporary files, recursion, settling, too large, a symlink
    out of the root;
  - check: counts, root unset, folder missing.
- **Database (`api/tests/db/test_csv_dir_ingest.py`):**
  - a scan ingests long CSV, wide CSV and Parquet (series, cache rows, coverage, file rows);
  - a rescan skips; a touched but unchanged file is known; a changed file is re-read and wins;
  - a failed file beside a good one gives `partial`;
  - the file and series caps;
  - a poll queues an ingest; a manual fetch is 409; a dataset run does not fetch;
  - routes: roles, the files list with its cursor and status filter, 404 across workspaces;
  - both routes in `test_rls.py`.
- **Web (`web/src/__tests__`):** the `csv_dir` form; the source page with Check, Scan now and
  Files, without Fetch or Search.

## Out of scope

- Moving or deleting processed files (needs write access to the folder).
- Other formats (Excel, JSON, COMTRADE) and compressed files.
- Watching the folder with inotify; the poll interval is the latency (minimum 60 s).
- Per-file metadata such as units from a header row; `PATCH /api/series/{id}` sets them.
