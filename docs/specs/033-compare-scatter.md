# Spec 033 — Compare view: scatter of one series against another

Sprint 10, story S10-7 (the follow-up spec 018 named). Depends on:
- spec 018 (compare view, its URL state, findings and the residual kernel);
- spec 008 (`align`, the grid the cross-series checks see);
- spec 006 (the Parquet cache), spec 025 (window helpers and the row limit).

Packages: `core/tabayyun-core` (a `scatter` kernel next to `residual`), `core/tabayyun-py`
(its binding), `api/src/tabayyun/` (route and service), `web/src/` (the scatter panel of
`/compare`), `docs/`.

## Goal

On the compare view a user plots one series against another: every point is one bin of the
grid the cross-series checks use, x from the first series and y from the second, coloured by
its time. Points inside finding windows are marked, so a correlation break shows as a part
of the cloud that leaves the rest. The server caps the number of points.

## User story

As a network analyst, I open the finding "T-12 and T-13 stopped moving together" and switch
on the scatter: the points of the week lie on one line, and the points of the break, ringed
and in the colour of last Tuesday, form a second cloud beside it. I see that the relation
changed and when, without reading two lines against each other.

## Interface

### Core

`tabayyun_core.scatter(tables, metas, x, y, from_ns, to_ns, grid="auto", max_points=2000)`
(Rust: `scatter::scatter(x, y, grid_ns, from, to, max_points)`) returns

```
{grid_ns, n_bins, r, ts: [..], x: [..], y: [..], missing: [id]}
```

- The two series are aligned by `align` on `[from, to)`: a value is the mean of the usable
  (good or estimated), finite samples of its grid bin. `grid` is `auto` (the coarser
  expected interval of the two) or a duration.
- A point exists for every bin in which both series have a value; `n_bins` is their number.
- `r` is the Pearson correlation over all those bins, not over the points returned; it is
  null with fewer than 3 bins or when a series is constant.
- At most `max_points` points are returned, in time order. With more bins, the bins are cut
  into consecutive chunks of equal length in time order, and each chunk gives up to two
  points: its middle bin, and the bin farthest from the chunk's centre in units of the
  window's x and y ranges. The first keeps the cloud's density over time, the second keeps
  what leaves the cloud; a short excursion therefore stays visible.
- `missing` lists a series without a usable, finite sample in the window; the rest is then
  empty. Pure: no I/O.

### API

`GET /api/compare/scatter?x=<series id>&y=<series id>&from=&to=&grid=&max_points=2000`
(viewer) → 200:

```json
{
  "window": {"from": "…", "to": "…"},
  "grid_ns": "900000000000",
  "x": {"id": "…", "name": "T-12", "unit": "°C"},
  "y": {"id": "…", "name": "T-13", "unit": "°C"},
  "n_bins": 672, "r": 0.93,
  "ts": ["…"], "xs": [20.1], "ys": [20.4],
  "missing": []
}
```

- `x` and `y` are two different series of the workspace: 422 when one is missing, not a
  UUID or both are the same; 404 naming the unknown ids, as for the compare chart.
- `from` and `to` as for the compare chart (ns strings; default the last 7 days of the two
  series' data). `window` is null, and both series are `missing`, when neither has data.
- `grid` is `auto` (default) or a duration such as `15m`; anything else is 422.
- `max_points` is 100 to 5000, default 2000.
- A window whose two series together hold more rows than `TABAYYUN_CHART_MAX_ROWS` is 422
  `window_too_large`. A window that needs more grid bins than `align` builds returns
  `grid_ns` and no points.
- `grid_ns` is a string like every ns value; `"0"` when the series give no grid.
- 401 without a session, 404 without a role in the workspace.

### Web

`/compare` gets the URL key `xy=<x id>,<y id>`; both ids must be among `series`.

- The controls get the switch "Scatter" (a pressed button while it is on): it sets `xy` to
  the selected finding's pair when the finding names exactly one partner among the series,
  otherwise to the first two series. "Hide scatter" in the panel, or the switch, removes it.
- The panel sits under the chart and uses the chart's window, so a zoom there redraws it.
  Two selects choose the x and the y series among the compared ones; "Swap" exchanges them.
- The plot: x and y axes labelled with name and unit, one dot per point, coloured from the
  window's start (blue) to its end (orange) on one ramp whose lightness changes in one
  direction, with a legend that names both dates. Points whose bin overlaps the window of a
  finding about either series (attached to it, or naming it as member or partner) are
  ringed; the selected finding's points are drawn last and stronger.
- Under the plot: "672 bins on a 15 min grid, 400 shown · r = 0.93 · 37 inside finding
  windows", and a table alternative (time, x, y, in a finding) of the points shown.
- States: loading; a missing series ("T-13 has no usable samples in this window"); fewer
  than 2 points; the error of the request.
- A point's time, x and y show on hover of its dot. In the table, the row pointed at or
  focused (one tab stop, the arrow keys move through the rows) is marked in the plot and
  read out under it.
- While another window or pair loads, the last cloud stays, dimmed and marked "Updating…".

## Behaviour

1. **Same grid as the checks.** The scatter aligns exactly as `tby.correlation_break` and
   the residual lane do, so a finding's evidence and the picture agree.
2. **Window.** The window is half-open; a bin that starts before it is kept with the samples
   inside the window only, and its time is clamped to the window start.
3. **Cap.** The answer never holds more than `max_points` points; `n_bins` says how many
   bins there were. The cap is deterministic: the same request gives the same points.
4. **Finding windows** are applied by the page from the findings it already loads (spec
   018); the API does not mark points.
5. **Tenancy.** Series of another workspace or org are unknown (404).

## Acceptance criteria

- [x] The core returns one point per complete grid bin, `r` over all bins, and at most
      `max_points` points that keep an excursion shorter than a chunk.
- [x] `GET /api/compare/scatter` returns the points on the checks' grid for the window, with
      the default window, `grid`, the row limit and the 422 and 404 cases above.
- [x] `/compare?…&xy=a,b` shows the scatter coloured by time with a legend, rings the points
      inside finding windows and emphasises the selected finding's.
- [x] "Scatter" picks the selected finding's pair; the selects and "Swap" change `xy`; a
      zoom of the chart redraws the scatter for the new window.
- [x] A correlation break on synthetic data shows apart from the cloud: the points of the
      break lie off the line of the others (API), and the points inside a finding's window
      are the ringed ones (web).
- [x] The table alternative lists the points shown; missing series and too few points have
      their own text.

## Test cases

- **Core** (`scatter.rs`): `one_point_per_complete_bin`, `pearson_over_all_bins`,
  `cap_keeps_a_short_excursion`, `cap_is_deterministic_and_ordered`,
  `missing_series_reported`, `window_and_grid`, `invalid_arguments_are_errors`.
- **Bindings**: `test_scatter_points_and_cap`.
- **API, no database** (`api/tests/test_compare_service.py`): `scatter_points`,
  `scatter_missing`.
- **API, database** (`api/tests/db/test_compare_api.py`): `scatter_of_a_pair`,
  `scatter_default_window_and_grid`, `scatter_validation`, `scatter_other_workspace`,
  `scatter_shows_a_correlation_break`.
- **Web** (Vitest): `compare.scatter` (colour ramp, finding marks, scales, summary text),
  `compare.url` (`xy`), `compare.page` (panel, selects, swap, states, table).

## Implementation notes

- The cap takes two points per chunk of consecutive bins: the middle one and the one
  farthest from the chunk's centre. A plain stride would drop an excursion shorter than its
  step, which is the thing the scatter is opened for; taking only extremes would draw the
  cloud wider than it is.
- `r` is computed before the cap, over every bin, so it does not change with `max_points`.
- A bin whose mean overflowed is not finite and counts as a bin without a value, as in
  `align`; every value in the answer is finite.
- The page marks finding windows itself from the findings it already loads. Windows are
  half-open like the chart's shading: a bin that only touches a window's end is outside.
- The colour ramp runs from blue to orange (the pair TrendMiner uses for time) instead of
  one hue: with a single blue ramp the points of a break in the middle of the window did
  not stand out from the rest. Every step keeps 3:1 against the panel in both themes.
- The switch in the controls keeps its name "Scatter" and reports its state as pressed; the
  table's button is "View the scatter as a table", because the chart has "View as table".
- Seen in Chromium against a local api, light, dark and at phone width, with two uploaded
  series of which one stops following the other for eight hours: the break is a second
  branch beside the line, in the colour of its time; 2 880 bins gave 1 859 points.
- Not exercised: a `tby.correlation_break` finding from a real dataset run opened in the
  browser (the tests ring points with a finding fixture), and the scatter on staging's data.

## Out of scope

- A regression line, a density view and brushing points back onto the time chart.
- More than one pair at a time, and a scatter matrix of all compared series.
- Lagged scatter (x against y shifted in time): with the lag estimate of spec 011, later.
- Scatter on corrected layers: with sprint 11.
