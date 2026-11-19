# Spec 029 — Findings inbox: triage across series

Sprint 10, story S10-3. Depends on:
- spec 003 and ADR-0013: findings, their lifecycle and deduplication;
- spec 007: tenancy and roles; spec 014: the audit log; spec 002: periodic tasks.

Packages: `api/src/tabayyun/` (model, migration 0015, service, routes, task), `web/src/`
(findings page).

## Goal

One page shows the findings of a workspace across all series, filtered, grouped and worked
through: acknowledge, mute until a date with a reason, resolve, assign to a colleague, mark as
a false positive, one at a time or many at once. Filters can be saved, the list can be
exported as CSV, and every action on a finding is stored and written to the audit log.

## User story

As a data steward, I open "Findings" in the morning. I pick my saved filter "Open, critical,
mine", group by series, acknowledge the three I am looking at, mute the known sensor swap on
feeder 4 until Friday with a reason, and hand the pressure findings to a colleague. A month
later the audit log still says who muted what and why.

## Interface

### Database (migration 0015)

`findings` gains:

| Column | Type | Meaning |
|---|---|---|
| `muted_until` | timestamptz, null | when a muted finding opens again; null mutes without an end |
| `assignee_id` | uuid, null | the user it is assigned to; cleared when the user is deleted |
| `feedback` | text, null | `false_positive`, or null |
| `feedback_at` | timestamptz, null | when the feedback was given |

`finding_filters` (tenant table, row-level security by `org_id`): `id`, `user_id` (deleted
with the user), `name` (1 to 80 characters, unique per user and workspace), `query` (the
page's URL search string, at most 2000 characters), `created_at`.

`tabayyun_reopen_expired_mutes(now)` (SECURITY DEFINER, callable by `tabayyun_app` only)
opens every muted finding whose `muted_until` has passed and returns how many.

### API

`GET /api/findings` keeps its parameters and gains:

| Parameter | Meaning |
|---|---|
| `assignee` | `me`, `none`, or a user id |
| `feedback` | `false_positive` or `none` |
| `q` | text contained in the summary or the series' name, any case (at most 200 characters) |
| `asset` | an asset path and everything below it |
| `asset_path` | exactly this asset path; empty for series without one |
| `source_id` | series of this source |

Each finding in a response gains `series_name`, `asset_path`, `assignee`
(`{"id", "display_name"}` or null), `muted_until` and `feedback`.

- `GET /api/findings/groups?by=check|series|asset` with the same filters →
  `{"items": [{"key", "label", "count", "severities": {"critical": n, …}, "latest": ns}],
  "truncated": bool}`, largest group first, at most 500 groups. `key` is the check id, the
  series id or the asset path (empty for none); it is the value of `check_id`, `series_id`
  or `asset_path` that lists the group.
- `PATCH /api/findings/{id}` (editor) takes any of `status` with `reason` and `muted_until`,
  `assignee_id` (a user id or null) and `feedback` (`false_positive` or null); at least one.
- `POST /api/findings/bulk` (editor): `{"ids": [1 to 500 ids], "change": {…as PATCH…}}` →
  `{"updated": [ids], "failed": [{"id", "error", "status"}]}` with `error` `not_found` or
  `transition`.
- `GET /api/findings/assignees` (viewer) → `{"items": [{"id", "display_name"}]}`: the users
  with at least the editor role in the workspace, by name.
- `GET /api/findings/export` (viewer) with the same filters → `text/csv`, at most 50 000
  rows, newest window first; the header `X-Tabayyun-Truncated: true` says the list was cut.
- `GET /api/finding-filters` → `{"items": [{"id", "name", "query", "created_at"}]}`, the
  caller's own in this workspace, by name. `POST /api/finding-filters` `{"name", "query"}` →
  201; 409 `name_taken`; at most 50 per user and workspace (409 `too_many`).
  `DELETE /api/finding-filters/{id}` → 204; 404 for another user's.

### Web

`/findings` ("Findings" in the navigation):
- a filter bar (status, severity, dimension, check, assignee, feedback, text) whose state is
  the URL, a menu of saved filters with "Save current filter" and delete;
- grouping by none, check, series or asset: groups with their count and severities, opened
  one at a time;
- a virtualised table (window, series, check, severity, status, assignee, summary) that loads
  further pages while scrolling, with row selection and "select all loaded";
- actions on the selection for editors: acknowledge, mute (reason and an optional end),
  resolve (optional reason), reopen, assign, false positive; the result names what could not
  be changed;
- "Export CSV" of the current filter; each row links to the series page.

## Behaviour

1. **Lifecycle** stays that of ADR-0013 (`open → acked | muted | resolved`, `acked →
   resolved | open`, `muted → open`, `resolved → open`). `muted_until` is accepted only with
   `status: muted`, must lie in the future and at most 366 days ahead; any other status
   change clears it.
2. **Expired mutes.** Every five minutes a periodic task calls the function: an expired
   mute becomes `open` with the reason `mute expired`.
3. **Assignment.** The assignee must hold at least the editor role in the workspace (422
   `assignee` otherwise); null unassigns. Assignment does not change the status.
4. **Feedback.** `false_positive` marks the finding and does not change its status; null
   removes the mark. The mark stays when the finding is resolved or reopened.
5. **Bulk.** Each finding is changed on its own terms: one whose lifecycle forbids the move
   is reported in `failed` and the others are still changed. Validation errors of the change
   itself (an unknown assignee, a past `muted_until`) refuse the whole request with 422.
6. **Audit.** Every change of a finding writes one event per finding with the acting user:
   `finding.status_changed` (`from`, `to`, `reason`, `muted_until`), `finding.assigned`
   (`from`, `to`), `finding.feedback` (`from`, `to`). A change that changes nothing writes
   none. Expired mutes are logged by the worker, not audited: no user acted.
7. **Saved filters** are personal: viewers may save them, nobody else sees them, and they
   are not audited.
8. **CSV.** Cells that start with `=`, `+`, `-` or `@` are prefixed with `'`, so a
   spreadsheet does not run them as formulas.

## Acceptance criteria

- [x] The list filters by assignee, feedback, text, asset and source, alone and combined
      with the existing filters, and returns series name, asset path and assignee.
- [x] Groups by check, series and asset count what the list returns for the same filter, and
      a group's key lists exactly that group.
- [x] Muting with an end stores it; a past or too distant end, or an end with another
      status, is a 422; the periodic task reopens expired mutes and no others.
- [x] Assigning to an editor works, to a viewer or a user of another org is a 422, and null
      unassigns.
- [x] `false_positive` is stored without a status change and can be removed.
- [x] A bulk change over findings in different states changes those it may and reports the
      others; more than 500 ids is a 422.
- [x] Every action writes its audit event with the acting user; viewers get 403 on every
      write to a finding.
- [x] The export contains the filtered findings as CSV with safe cells and says when it was
      cut.
- [x] Saved filters are created, listed and deleted per user; another user neither sees nor
      deletes them.
- [x] The page filters through the URL, groups, selects, performs every action on a
      selection, saves and applies filters, and stays responsive with 10 000 loaded findings.
- [x] Another org's findings and filters give 404, also inside a bulk request (`not_found`).

## Test cases

- **API** (`api/tests/db/test_findings_inbox.py`): `filters_and_response_fields`,
  `groups_match_the_list`, `mute_until_rules_and_expiry`, `assignment_rules`,
  `feedback_does_not_change_status`, `bulk_changes_what_it_may`, `audit_and_roles`,
  `export_csv_safe_and_truncated`, `saved_filters_are_personal`.
- **Tenancy**: the new routes join `tests/db/test_rls.py`; migration 0015 upgrades and
  downgrades; the function is callable by the app role only.
- **Web** (Vitest): `findings.inbox` (filters in the URL, grouping, selection and each bulk
  action with its dialog, partial failure message, saved filters, export link, viewers see
  no actions), `findings.virtual` (only the visible rows are rendered).

## Implementation notes

- `PATCH /api/findings/{id}` keeps its old body (`status`, `reason`) and now refuses unknown
  fields; its lifecycle errors are unchanged. The service function behind it moved to
  `services/triage.py`, which records every change.
- Saved filters store the page's search string as it is, grouping included, so a filter can
  open the page grouped.
- The assignee list holds the users with at least the editor role, by name, without e-mail
  addresses: viewers may read it.
- Groups are counted in one query with a count per severity; a group's `key` is passed back
  as `check_id`, `series_id` or `asset_path`.
- The table is virtualised with `@tanstack/react-virtual` at a fixed row height; the React
  compiler leaves that component unmemoised, which the linter reports as a notice.
- More than 500 selected findings are sent in requests of 500, one after another.
- "Stays responsive with 10 000 loaded findings" is shown by the test that only a screenful
  of rows is in the document; it was not measured in a browser with that many.
- A run interrupted by a usage limit left the web half unfinished overnight; it was completed
  and verified in the next run.

- From the review:
  - Assignee, feedback and a mute's end survive when a later report merges into the finding,
    also when its window grows backwards and the row is rewritten.
  - The CSV uses CRLF line ends, so a cell with a carriage return is quoted and cannot break
    a record; cells that start with a tab or a carriage return are prefixed too.
  - When a later request of a bulk change over more than 500 findings fails, what was
    changed is reported and reloaded, and the findings whose request was not sent stay
    selected.
  - A dialog stays open, with Cancel disabled, while its request runs.
  - The stored selection is pruned whenever the loaded findings change, so a finding that
    left the list and came back is not selected again.
  - A value that replaces the search text from outside drops a commit of typed text that
    was still waiting.
  - A bulk request without an answer of the server (a lost response, a 5xx) has an unknown
    outcome. The page then asks for each of its findings by id, eight at a time, whatever
    the filter shows, and says what each is now, not what the request did (a lookup cannot
    tell): as the change asked (status with the reason and the end that were sent,
    assignee, feedback), not showing the change (it stays selected), or gone. One that
    cannot be looked up either is reported as unknown. Findings whose request was never
    sent are named apart. Whatever the result says stays selected does so until the next
    change, also when the list no longer shows it, so trying again tells what became of
    it. Only a refusal (4xx) of the first request is shown as an error; it is an answer
    and starts no lookup. The list is reloaded in every case.
  - One bulk change at a time: while one runs, the checkboxes are disabled and the actions
    stay on the page, so no second change can start beside it and a refusal has a place to
    show. The findings of the running change stay selected until its result is reported,
    also when a reload drops them in between; "select all loaded" adds and removes the
    loaded rows only.

## Out of scope

- Live updates of the page: S10-4 (SSE).
- Alerts on findings: S10-5.
- Comments on findings and a history view per finding: the audit log holds the history.
- Using false-positive feedback to tune thresholds: later, with packs (S12).
- A detail drawer with evidence: rows link to the series page, which shows the finding.
