# Spec 025 — Series page with chart

Sprint 9, story S9-5. Depends on: 004 (series API), 006 (Parquet cache and coverage), 003
(findings), 024 (catalogue). Packages: `core/` (chart kernel and binding), `api/` (chart and
profile routes), `web/` (series page).

## Goal

A series has a page. It shows the series' values as a fast chart, with its findings shaded,
its quality marked under the chart, and its operating band and physical limits drawn. Beside
the chart are its metadata and its profile. The chart reads M4-downsampled points from the
API, so a series of a million points opens at once. Zoom, pan and preset ranges re-query.
The range also works from the keyboard and through date inputs.

## User story

As an engineer, I open a series from the catalogue, see where its findings sit in its values,
zoom into one, and see whether the samples there were bad quality, without exporting data.

## Interface

### Core

| Function | Behaviour |
|---|---|
| `downsample::m4_window(frame, from, to, buckets)` | M4 on `buckets` equal bins of `[from, to)`. Bins come from the window, not the data, so two series asked for the same window share bins (spec 018). Up to four points per bin (first, min, max, last), in time order, without duplicates. |
| `downsample::gap_breaks(ts, values, max_step)` | Inserts a NaN point one nanosecond after any sample followed by a step longer than `max_step`, so the chart breaks the line there. |
| `downsample::quality_runs(frame, from, to, buckets)` | Runs `(start, end, quality)` of non-good samples. Each bin with a non-good sample takes its worst class (bad > estimated > uncertain). Consecutive such bins of one class merge unless a good sample lies between them, so sparse samples (fewer than one per bin) still make one run. A run spans its bins, within the window. |
| `tabayyun_core.chart(data, from_ns, to_ns, buckets)` | Python binding. Returns `{"ts": [...], "values": [...], "quality": [(start, end, name)], "n_raw": n}`, releasing the GIL. The data needs columns `ts`, `value` and `quality`. |

- Gap breaks use `max_step = 2 × bin width`, or 3 × the series' expected interval when that
  is longer.
- Rows outside `[from, to)` are ignored.
- NaN samples count in `n_raw`, are never min or max, and draw no line.

### API

| Route | Response |
|---|---|
| `GET /api/series/{id}/chart?from=&to=&width_px=&layer=raw` | `{window: {from, to} \| null, extent: {start, end} \| null, layer, width_px, n_raw, ts: [ns strings], values: [number \| null], quality: [{start, end, quality}]}` |
| `GET /api/series/{id}/profile?from=&to=&layer=raw` | `{window: {from, to} \| null, profile \| null, quality_counts: {good, uncertain, bad, estimated}, band: {lo, hi, source} \| null}` |

- **Time parameters:**
  - `from` and `to` are RFC 3339 or epoch ns, as elsewhere in the API.
  - Every time in a response is a string of ns since the epoch.
  - `null` stands for NaN.
- **`layer`:** only `raw` exists (ADR-0010). Another value is 422.
- **`width_px`:** 100–4000, default 1200. The bins are `width_px` buckets, so the response
  holds at most `4 × width_px` points plus the gap breaks.
- **`extent`:** the series' covered range from `coverage` (earliest start, latest end).
  It is null when nothing is cached; then `window` is null too unless `from` and `to` were given.
- **Default chart window:** when `from` and `to` are both absent, the window is the whole
  extent if coverage records at most 2 000 000 rows. Otherwise it is the latest 7 days of
  the extent. Giving only one of them is 422.
- **Row cap:** a window whose coverage estimate exceeds `TABAYYUN_CHART_MAX_ROWS` (default
  10 000 000) is answered 422 `window_too_large`. The estimate sums each coverage range's rows,
  scaled by the share of the range inside the window.
- **Caching:** `ETag` is a hash of the parameters, the resolved window and the latest
  coverage write. A request with a matching `If-None-Match` gets 304 without reading the
  cache. Responses are `Cache-Control: private, no-cache`.
- **Profile window:** the default is the trailing 28 days of the extent.
- **Profile contents:**
  - `profile` is the core profile of the window's samples, without bad-quality samples, as
    the catalogue defines a baseline (`docs/checks/catalogue.md`). It is null when no usable
    sample is left.
  - `quality_counts` counts all samples in the window.
- **Band:** the series' operational range when both ends are set (`source: "metadata"`).
  Otherwise it is `[p0.1 − MAD, p99.9 + MAD]` from the profile (`source: "profile"`), the
  formula and default `k = 1` of `tby.operational_range`. It is null when neither is
  available. Stored, versioned baselines replace the on-the-fly profile in S10-2.
- **Errors:**
  - an unknown or foreign series is 404;
  - a store that cannot be read is 503 `cache_unavailable`.

### Web

| Route | Who | Content |
|---|---|---|
| `/series/$seriesId` | viewer | header, range bar, chart with findings, band, limits and quality rug; findings in the window; metadata and profile panel |

- **URL search:** `from` and `to` (ns strings), and `finding` (a finding id). With neither
  `from` nor `to`, the server's default window applies.
- **Header:**
  - name, external id, source (linked to its page), unit and kind;
  - latest score, open findings and last run.
- **Range bar:** presets "1 h", "1 d", "7 d", "30 d" (ending at the extent's end) and "All";
  From and To inputs; Back (the previous range).
- **Chart:**
  - **Data:** a uPlot line of the M4 points.
  - **Findings:** each finding window is a shaded rectangle coloured by severity. The
    selected one is outlined.
  - **Band:** a translucent horizontal strip, labelled with its source.
  - **Physical limits:** dashed lines.
  - **Quality rug:** a strip under the plot, coloured by quality.
- **Interaction:**
  - Dragging a span zooms to it.
  - Double-click goes back to the previous range.
  - Keys on the focused chart:
    - ← and → pan by half the window;
    - `+` and `−` zoom in or out by 2× around the centre;
    - Home shows all.
- **Findings list:** findings that overlap the window, newest first, with severity, check,
  window and summary. Selecting one zooms to its window plus a quarter on each side and sets
  `finding`.
- **Side panel:**
  - metadata (interval, limits, resolution, asset path, extra metadata);
  - profile (count, interval, median, MAD, p0.1 and p99.9, resolution, quality mix, period);
  - the band and where it comes from.
- **Links:** catalogue rows and the run report's series card link to `/series/$seriesId`.

## Behaviour

1. **Opening.** The page loads the series, then the chart for the URL's range (or the default)
   at the chart's width in CSS pixels. Findings for the chart's window come from
   `GET /api/findings?series_id=&since=&until=&status=all&limit=500`, so muted and resolved
   ones are shaded too, with their status in the list. The profile loads alongside.
2. **Empty.** With `extent: null` the page says that no data is cached yet. For a series of
   a connector source it names the source's Fetch action.
3. **Zoom.**
   - Any range change replaces the URL search and re-queries.
   - The previous chart stays on screen until the new one arrives.
   - A range narrower than 1 s is widened to 1 s around its centre.
4. **Too large.** On `window_too_large` the page keeps showing the previous chart and says to
   zoom in.
5. **Gaps.** A null in `values` breaks the line. Spans with no samples show no line, and the
   rug shows no colour.
6. **Selection.** `finding` in the URL outlines that finding and scrolls its row into view. It
   is ignored when the finding is not in the window.

## Acceptance criteria

- [x] `m4_window` uses window bins; spikes survive; two frames share bin edges;
  `quality_runs` merges runs and picks the worst class; `gap_breaks` breaks long steps.
- [x] `GET /api/series/{id}/chart` returns M4 points, extent, `n_raw` and quality runs. It
  applies the default window, the row cap, `layer` validation and ETag/304, and returns
  empty arrays when nothing is cached.
- [x] `GET /api/series/{id}/profile` profiles the window without bad samples, counts quality
  over all samples, and gives the band from metadata or the profile.
- [x] Both routes keep to the caller's workspace (RLS test list).
- [x] `/series/$seriesId` shows the header, chart, shaded findings, band, limits, quality
  rug, findings list, metadata and profile.
- [x] Range presets, inputs, drag zoom, back, and the ←, →, `+`, `−` and Home keys change
  the range in the URL and re-query.
- [x] Catalogue rows and the run report's series card link to the page.
- [x] A 1 000 000-point series renders under 500 ms after the first load. This is measured
  in Chromium and recorded below, and the API test times the chart endpoint on 1M points.
- [x] `make lint` and `make test` (or the relevant subsets) pass.

## Test cases

- **Core (`downsample.rs`):**
  - `m4_window_bins_from_window`;
  - `m4_window_keeps_spikes`;
  - `m4_window_shared_edges`;
  - `m4_window_ignores_outside_rows`;
  - `gap_breaks_inserts_nan`;
  - `quality_runs_merge_and_worst_class`.
- **Bindings (`core/tabayyun-py/tests`):** `chart` returns the documented keys and types.
- **API (`api/tests/db/test_series_chart.py`):**
  - window and points;
  - default window (small and large);
  - one-sided range → 422;
  - `layer=gold` → 422;
  - row cap → 422;
  - ETag → 304;
  - empty series;
  - profile without bad samples and quality counts;
  - band from metadata and from the profile;
  - 1M-point timing (logged, generous bound).
  - Both routes are added to `test_rls.py`.
- **Web (`web/src/__tests__`):**
  - `seriesChart.test.ts` (pure helpers): range maths, keys, presets, finding rectangles,
    rug segments, null handling;
  - `SeriesDetail.test.tsx`, with uPlot mocked:
    - header, panels and findings;
    - presets and keys update the URL and the request;
    - a finding click zooms and selects;
    - the empty state;
    - `window_too_large`;
  - catalogue and run report links.

## Implementation notes

- **Speed (criterion "1M points under 500 ms").** Measured in this container on 10 Oct 2026:
  - The API test reads 1 000 000 raw points from a local cache and answers the chart in
    274 ms, then 211 ms.
  - The core's `chart` alone takes 44 ms on about 1M rows in memory.
  - In Chromium, against the built app, with a mocked API serving a real M4 answer of a
    997 500-point series (4 199 points), opening the series from the catalogue paints the
    chart in a median of 137 ms over 7 runs.
  - With the chart answer delayed by the API's 274 ms, the median is 390 ms (at most 405 ms).
  - A range change then redraws in 58 ms, or 326 ms with that delay.
- **uPlot** loads as its own chunk (55 kB, 24 kB gzipped). The page starts that download as it
  mounts, alongside its API requests. Waiting until the chart data arrived cost about 280 ms.
- **Quality runs** first merged only bins that touch. Hourly samples in 43-minute bins then
  made a bad stretch a row of dashes, so runs now merge until a good sample lies between them.
- **Gap breaks and the ETag.** The ETag includes the series' `updated_at`, because the
  declared interval moves the gap breaks.
- **Band.** The band and the profile are computed per request until S10-2 stores baselines.
  The band uses `tby.operational_range`'s formula with its default `k = 1`.
- **Tests and uPlot.** All uPlot calls go through `components/chart/uplot.ts`, which the page
  tests replace, because jsdom has no canvas.

## Out of scope

- Stored, versioned baselines and their band (S10-2).
- The compare view (S9-7, spec 018), which reuses `m4_window`.
- Editing metadata on this page; `PATCH /api/series/{id}` exists.
- MinMaxLTTB, layers other than raw, and live updates (S10-4).
