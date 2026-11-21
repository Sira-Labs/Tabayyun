# Spec 032 — Overview: scores, worst series, findings over time, source health

Sprint 10, story S10-6. Depends on:
- spec 002 (runs and scores), spec 008 (dataset runs), spec 007 and 014 (tenancy and roles);
- spec 021 and 024 (sources and their health), spec 029 (findings inbox, the page the
  overview links into), spec 030 (live events).

Packages: `api/src/tabayyun/` (overview service and route, the daily score table, migration
0018), `web/src/` (the overview page at `/`), `docs/`.

## Goal

The first page after sign-in answers "how is the data of this workspace": one tile per
quality dimension with its level and its course over the last weeks, the series that are
worst right now, how many findings appeared per day by severity, and whether the sources
are reachable. Each part links to the page that shows the detail. The page loads in under a
second for a workspace of 10 000 series.

## User story

As a data steward, I open Tabayyun in the morning and see at a glance that completeness
dropped since Tuesday, which twenty series pull it down, that yesterday brought forty new
high findings and that one connector has not answered since the weekend, without opening
four lists.

## Interface

### Database (migration 0018)

`score_days` (tenant table, row-level security by `org_id`): the last score of each series on
each UTC day. The overview reads it instead of `scores`, which holds one row per run.

| Column | Type | Meaning |
|---|---|---|
| `series_id` | uuid | the series; its deletion deletes its days |
| `layer` | text | `raw` (as in `scores`) |
| `day` | date | the UTC day of `computed_at` |
| `overall` | double | the overall score |
| `completeness`, `timeliness`, `validity`, `accuracy`, `consistency`, `plausibility`, `integrity` | double, null | the dimension scores; null when the score has none |
| `n_findings` | integer | findings the score counted |
| `computed_at` | timestamptz | when that score was computed |

Primary key `(series_id, layer, day)`; index `(workspace_id, layer, day)`. The migration
fills the table from `scores`. `findings` gets the index `(workspace_id, created_at)`.

### API

`GET /api/overview?days=30&asset=<prefix>&source=<id>` (viewer) → 200:

```json
{
  "window": {"days": 30, "start": "2026-10-23", "end": "2026-11-21"},
  "series": {"total": 10000, "scored": 9950},
  "tiles": [
    {"metric": "overall", "mean": 96.4, "min": 41.0, "n_series": 9950,
     "trend": [{"day": "2026-10-23", "mean": 97.1, "n": 800}]}
  ],
  "worst": {
    "share": 0.05, "n": 498,
    "items": [{"series_id": "…", "name": "Feeder 4 pressure", "asset_path": "plant/feeder-4",
               "overall": 88.2, "metric": "completeness", "value": 41.0,
               "open_findings": 12, "computed_at": "…"}]
  },
  "findings": {
    "open": {"critical": 1, "high": 14, "medium": 40, "low": 3},
    "days": [{"day": "2026-10-23", "critical": 0, "high": 2, "medium": 5, "low": 0}]
  },
  "sources": [{"id": "…", "name": "Historian", "type": "pi_web_api", "enabled": true,
               "status": "ok", "n_series": 4200,
               "last_fetch": {"id": "…", "status": "succeeded", "finished_at": "…", "rows": 120}}]
}
```

- `days` is 7, 30 or 90 (default 30); anything else is 422. The window is the last `days`
  UTC days including today.
- `asset` keeps the series whose asset path starts with it (at most 200 characters);
  `source` keeps the series of one source (422 when it is not a UUID). Both apply to
  `series`, `tiles`, `worst` and `findings`; `sources` always lists every source.
- `tiles` has eight entries in a fixed order: `overall`, then the seven dimensions.
  `mean` and `min` are over each series' newest day in the window, `n_series` is how many
  series have a value; all three are null or 0 without data. `trend` has one entry per day
  on which any series was scored: the mean over the series scored that day and their number.
- `worst`: the series are ranked by their lowest value among overall and the dimensions,
  lowest first (ties: lower overall, then name). `n` is 5 % of the scored series, rounded
  up; `items` are the first 50 of them, each with the metric that is lowest and its value,
  and the number of its unresolved findings.
- `findings.open` counts the unresolved findings (open and acknowledged) by severity;
  `findings.days` has one entry per day of the window, zeros included, with the findings
  first stored that day (`created_at`) by severity.
- `sources[].last_fetch` is the source's last data fetch as the sources API gives it, or null.
- 401 without a session, 404 without a role in the workspace.

### Web

`/` is the overview (it redirected to `/runs` before); the navigation starts with
"Overview". The filters live in the URL: `/?days=7&asset=plant/&source=<id>`.

- A filter bar: window (7, 30, 90 days), source (a select of the workspace's sources) and
  asset prefix (text, applied on Enter or when the field is left).
- Eight score tiles: metric, mean with one decimal coloured as the existing score tiles,
  the lowest value, the number of series, and a sparkline of `trend` with a text
  alternative ("from 97.1 to 96.4 over 30 days").
- "Worst series": a table of `items` (name linking to the series, asset, the lowest metric
  and its value, overall, unresolved findings linking to the inbox filtered to that series)
  and a line "50 of 498 shown" when there are more.
- "New findings per day": a stacked bar per day by severity with a table alternative, and
  the unresolved counts by severity, each linking to the inbox with that filter.
- "Sources": one chip per source with name, health and series count, linking to the source;
  disabled sources are marked.
- The page reloads on the live events of runs and findings (spec 030).
- Empty workspace: a line that says nothing was scored yet, with a link to "New run".

## Behaviour

1. **Keeping the days.** The transaction that stores a score (upload run and dataset run)
   also writes that series' row for the score's UTC day, replacing an older score of the
   same day. A run that is rolled back leaves no row.
2. **Reading.** The route reads `score_days`, `findings`, `series` and `sources` of the
   workspace in one transaction; it never reads `scores`.
3. **Trend.** A day's mean is over the series scored on that day, not over all series: with
   suites that run at different times a day can hold a part of the workspace, which is why
   every point carries its number of series.
4. **Worst series.** The rank uses the lowest of a series' values, because the overall score
   is a weighted mean that a single bad dimension hardly moves.
5. **Filters.** Unknown query parameters are ignored; a filter that matches nothing gives
   the same shape with zeros and empty lists.
6. **Tenancy.** Everything is restricted to the workspace of the request; another org's
   source id as `source` matches nothing.
7. **Speed.** With 10 000 series, 30 days of daily scores (300 000 rows) and 50 000
   findings the route answers in under a second.

## Acceptance criteria

- [x] A run's score appears in `score_days` for its day; a second run the same day replaces
      it; a later day adds a row; the migration fills the table from existing scores.
- [x] `GET /api/overview` returns the tiles in order with mean, min, series count and the
      trend per day, for 7, 30 and 90 days; other values are 422.
- [x] The worst list ranks by the lowest value, names that metric, holds 5 % of the scored
      series (at most 50 listed) and counts unresolved findings.
- [x] Findings per day cover every day of the window with zeros, counted by `created_at`;
      the unresolved counts by severity match the inbox.
- [x] `asset` and `source` narrow series, tiles, worst and findings; sources are listed in
      full with health and series count.
- [x] Another workspace or org sees none of this workspace's numbers; no role gives 404.
- [x] 10 000 series with 30 days of scores and 50 000 findings answer in under a second.
- [x] The web page shows tiles with sparklines, the worst series, findings per day and the
      sources, keeps its filters in the URL and reloads on live events.

## Test cases

- **API, no database** (`api/tests/test_overview.py`): `summarise_tiles`,
  `rank_worst_by_lowest_value`, `fill_days_with_zeros`, `window_of_days`.
- **API, database** (`api/tests/db/test_overview.py`): `run_writes_its_score_day`,
  `overview_tiles_and_trend`, `worst_series_and_open_findings`, `findings_per_day`,
  `filters_by_asset_and_source`, `sources_strip`, `validation_and_roles`,
  `other_workspace_and_org_see_nothing`, `ten_thousand_series_under_a_second`.
- **Tenancy**: `score_days` joins `EXPECTED_TABLES`; migration 0018 upgrades (with the
  backfill), downgrades and upgrades again.
- **Web** (Vitest): `overview.page` (tiles, sparkline text, worst table and its links,
  findings table alternative, source chips, empty state), `overview.helpers`, `overview.filters` (filters from
  and to the URL, the request they cause), `overview.live` (reload on `run.completed`).

## Implementation notes

- `score_days` exists because `scores` holds a row per run: 10 000 series with hourly suites
  are millions of rows a month, and the overview must answer within a second.
- Each series' newest day is read with one primary-key probe per series (a lateral join).
  Sorting every day of the window for `DISTINCT ON` took 0.7 s with 300 000 rows; the probes
  take 0.13 s. The whole request measured about 0.5 s locally at that size.
- Unresolved means open and acknowledged, as on the series pages and as the inbox shows by
  default, so the page's links into the inbox carry no status filter.
- The inbox has no filter by series id; the worst list links to the inbox grouped by series
  with that series' group open.
- `sources[].last_fetch` is passed on as the sources API stores it (an object), not
  flattened to a time.
- An invalid `days` or `source` in the page's URL is ignored by the page (it asks for 30
  days), as the inbox does with its search; the API itself answers 422.
- Seen in Chromium against a local api with the example data, light and dark and at phone
  width: tiles with sparklines, the worst series, the stacked bars and the source chip; the
  filters wrote themselves into the URL.

## Out of scope

- Changing how scores are computed (severity weights, a penalty per episode): the backlog
  note "Scores that barely move" stays open for the scoring method; this spec answers its
  display half (the lowest dimension is shown and ranked by).
- Saved overview layouts, comparing two windows, and exporting the overview.
- An overview across workspaces (org level): after R1.
- Alerts on the overview's numbers: spec 031 covers scores per series.
