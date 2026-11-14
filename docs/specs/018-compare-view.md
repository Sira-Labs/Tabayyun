# Spec 018 — Compare view: several series on one chart

Sprint 9, story S9-7. Depends on:
- spec 008: series groups, datasets, `align`;
- spec 006: the Parquet cache;
- specs 009–011: the cross-series findings;
- story S9-5: the series chart endpoint and the uPlot chart component.

Packages: `api/src/tabayyun/` (routers, services), `core/tabayyun-core` (a residual kernel
next to `align`), `web/src/` (route, page, chart helpers). Research:
`docs/research/06-multi-series-comparison.md`.

## Goal

A user sees 2 to 8 series on one screen, on a shared time axis. Zoom and the cursor are
shared, and axes are grouped by unit. The view opens from a series group, a dataset, a
finding or a series page.

Cross-series findings are drawn across all series. A finding that names a suspect draws that
series stronger and labels it. Balances and pairs get a residual lane: inputs minus outputs,
or A − B, with the loss band. Everything shown is also available as a table.

## User story

As a network analyst, I open the finding "Balance SS-North does not close; F3 explains most of
it" and see the incoming feeder and the three outgoing feeders on one chart. The residual lane
leaves the loss band exactly while F3 drops, so I can confirm the meter fault before the
losses report goes out.

## Interface

### Web route

`/compare` takes its state in the URL, so a view can be shared:

| Query key | Meaning |
|---|---|
| `series` | comma-separated series ids, 2–8, in display order |
| `from`, `to` | window, ns since the epoch as decimal strings; default is the last 7 days of the members' data |
| `layout` | `auto` (default), `overlay` or `lanes` |
| `norm` | `raw` (default), `range`, `z` or `index` |
| `group` | series group id; adds the residual lane for balances and pairs, and orders series by role |
| `finding` | finding id; selects it: shaded, suspect emphasised, window centred on it |

Entry points (links that build the URL):
- a finding (on the series page and in the run report): the finding's series plus `members`
  or `partner` from its evidence, with `group` and the finding selected;
- a series page: "Compare with…", which picks up to 7 more series from the catalogue;
- the navigation: `/compare` without series offers the same picker.

Not built yet, because the web app has no page for either: a series group's page (all
members, up to 8) and a dataset (its first 8 series, with a note naming the rest). They get
their link when those pages exist; `group=` in the URL already works.

### API

- `GET /api/compare/chart?series=<id>&series=<id>…&from=&to=&width_px=`
  - Returns M4-downsampled points for 2–8 series, all on the same bins.
  - Response:
    ```
    {bins: {from, to, width_px},
     series: [{id, name, unit, physical_min, physical_max, ts: [..], values: [..], n_raw,
               stats: {min, max, mean, sd, first: {ts, value}} | null}]}
    ```
  - `ts` values are ns strings. `n_raw` is the number of raw points in the window.
  - Without `from` and `to`, the window is the last 7 days of the members' data; `bins`
    reports the window used. `from` and `to` are null when no member has data at all.
  - A window whose series together hold more rows than `TABAYYUN_CHART_MAX_ROWS` is a 422
    `window_too_large`, as for the series chart.
  - Chart points are M4 of every finite raw sample in `[from, to)`, whatever its quality, so
    bad-quality stretches stay visible (S9-5 marks them in its quality rug).
  - `stats` is computed on the raw, usable (good or estimated quality, as the checks count
    them), finite samples in `[from, to)`, before downsampling. `sd` is the population standard deviation. `first` is
    the earliest such sample. `stats` is null when the window holds none, which can happen
    while chart points exist (every finite sample is bad).
  - It reuses S9-5's M4 kernel and cache reads, one call instead of eight.
- `GET /api/series-groups/{id}/residual?from=&to=&width_px=`
  - For a `balance` group, or any group with exactly two members.
  - The members are aligned on spec 008's grid: each member's value in a grid bin is the
    mean of its usable (good or estimated), finite raw samples there, NaN when there are
    none (the same `align` as the checks). The grid is the group's `grid` param, as for
    `tby.balance_residual`: `auto` (the coarsest expected interval) or a duration.
  - The residual r = Σin − Σout (balance) or A − B (pair) is computed only for grid bins
    where every member has a value, as `tby.balance_residual` does. Incomplete bins are
    skipped and do not suppress the others. The result is then M4-downsampled on the same
    bins as the chart.
  - Response:
    ```
    {kind: "balance" | "pair", grid_ns,
     window: {from, to} | null,
     ts, residual,
     band: {lo: [..], hi: [..]} | null,
     missing: [series_id]}
    ```
  - `residual` holds null where the line breaks across skipped grid bins.
  - `band` is `loss_min·Σin` and `loss_max·Σin` from the group's params, or the
    `tby.balance_residual` defaults, one value per residual point: the band of that point's
    own grid bin. It is null for pairs.
  - `missing` lists members with no usable, finite raw sample anywhere in the window. When it
    is not empty, `ts` and `residual` are empty.
  - `window` is the window used (the default is the compare chart's); it is null, and every
    member is `missing`, when no member has data at all.
  - A group of another kind with three or more members is a 422, as is a balance whose
    `loss_min` is not below its `loss_max`.
- Findings for the view come from the existing `GET /api/findings?series_id=…&since=&until=`,
  one call per series. Cross-series findings are attached to a member (specs 009–011), so
  these calls return them too.

### Core

`tabayyun_core.residual(tables, metas, members, from_ns, to_ns, grid=…)` returns the aligned
residual per grid bin, plus Σin per bin for the band; `members` lists each series with its
sign (+1 for inputs and a pair's first member, −1 otherwise). With `buckets` it returns the
chart points instead, and with `loss` their band. It is pure, and uses the same `align` as
the checks. `tabayyun_core.compare_series` returns one series' chart points and `stats`.

## Behaviour

1. **Opening.** The page validates `series`.
   - Fewer than 2 or more than 8 series: the page says so and offers a picker.
   - Unknown ids: the page lists them.
   - The API answers 422 for the count and 404 for unknown ids.
2. **Axes (`layout=auto`).** Series are grouped by unit; series without a unit form their own
   group.
   - One or two groups: one plot, a left and a right y-axis.
   - Three or more groups: stacked lanes, one per unit group. Each lane is a uPlot instance,
     and all lanes share cursor, series toggles and zoom through `uPlot.sync`.
   - `overlay` forces one plot. `lanes` forces one lane per series.
3. **Normalisation.** Any `norm` other than `raw` puts every series on one dimensionless axis
   labelled with the mode.
   - The client applies the mode to the downsampled points using the raw-window `stats` of
     the chart response.
   - Every mode is a positive affine map (a·x + b with a > 0), so normalising the M4 points
     gives the same picture as M4 of the normalised raw samples.
   - Normalised values therefore do not depend on the chart width, and a zoom recomputes them
     from the new window's `stats`.
   - `range`: (x − lo) / (hi − lo). `lo` and `hi` are the physical limits when both are known,
     otherwise `stats.min` and `stats.max`. The mode is unavailable, with the reason shown,
     when hi = lo.
   - `z`: (x − `stats.mean`) / `stats.sd`. The page states that the visible window defines it.
     The mode is unavailable when sd = 0.
   - `index`: 100 × x / x₀, where x₀ = `stats.first.value`, the earliest usable raw sample in
     the window. The mode is unavailable when x₀ ≤ 0 or `stats.min` < 0 < `stats.max`: the
     series' sign changes, and a negative x₀ would flip the axis.
   - A series whose `stats` is null is drawn raw only: in a normalised mode it is hidden with
     the reason "no usable samples in the window" in its legend entry.
   - The cursor readout always shows raw values with their units.
4. **Downsampling.** One `compare/chart` call returns M4 points per series on identical bins.
   - The client outer-joins them (`uPlot.join`).
   - The readout shows, for each series, the point nearest the cursor inside the hovered bin.
   - A series with no finite sample in the window (empty `ts`) stays in the legend, marked
     "no data".
5. **Residual lane.** With `group` set to a balance or a two-member group, a lane under the
   plot shows the residual with a zero line. For balances it also shows the loss band as a
   uPlot band.
   - If a member has no usable value anywhere in the window (`missing`), the lane names it
     and no residual is drawn. Gaps in single bins only leave gaps in the residual line.
   - Groups of other kinds with three or more members get no residual lane.
6. **Findings.**
   - Each series' findings in the window are shaded in its lane, in the series colour.
   - Cross-series findings (evidence with `group_id` or `partner`) are shaded across all
     lanes, in the finding colour.
   - Selecting a finding, from the list under the chart or via `finding=`, does four things:
     - centres the window on it;
     - draws its `suspect` (or its series when there is no suspect) thicker, labelled
       "suspect";
     - mutes the other series to 40 % opacity;
     - shows its summary above the chart.
7. **Zoom and range.**
   - Brush-zooming in any lane re-queries `compare/chart` and `residual` with the new
     `from`/`to` and updates the URL.
   - Range inputs for the keyboard and a "reset" button do the same.
   - A double click zooms out to the previous window.
8. **Colour and marking.**
   - Series take the Okabe-Ito order, with a light-theme and a dark-theme variant. Each
     colour reaches 3:1 against its chart background (WCAG 1.4.11); colours that fail are
     darkened (light theme) or lightened (dark theme) until they pass.
   - Series 5–8 are also dashed.
   - Every line carries a direct label at its right end.
   - Legend entries are buttons that toggle a series, with the focus state shown.
9. **Table view.** "View as table" shows one row per bin, with each series' min and max in
   the bin, then the findings in the window.
   - The table can be exported as CSV.
   - Arrow keys step the chart cursor bin by bin and announce the readout in an ARIA live
     region.
   - The chart has an `aria-label` summarising the series, window and selected finding.

## Acceptance criteria

- [x] `GET /api/compare/chart` returns 2–8 series on identical bins, M4 per series.
- [x] `GET /api/compare/chart` answers 422 outside 2–8 series and 404 for unknown ids.
- [x] For a synthetic balance (1 in, 3 out, one outlet 30 % low for 6 h),
      `GET /api/series-groups/{id}/residual` returns a residual that leaves the band exactly
      over those 6 h.
- [x] For a pair, the residual is A − B, with no band.
- [x] A missing member is listed in `missing`.
- [x] Opening a `tby.balance_residual` finding shows all members and the residual lane, and
      draws the suspect thicker and labelled.
- [x] Opening a `tby.correlation_break` finding shows the pair.
- [x] Three units give stacked lanes that share cursor and zoom; two units give two axes on one
      plot.
- [x] `range`, `z` and `index` normalise as specified, from the raw-window `stats` rather than
      from the downsampled points. The result is identical at two chart widths. `index` is
      refused with a reason for a series starting at 0 or changing sign. The readout keeps raw
      values and units.
- [x] Every series colour reaches 3:1 against the light and dark chart backgrounds (unit test
      over the palette).
- [x] Series 5–8 are dashed.
- [x] The table view and keyboard cursor work without a mouse.
- [x] 8 series of 1M raw points each render under 1.5 s after the first load (S9-5's 500 ms
      budget per series, shared). Measured: see the implementation notes.
- [x] The URL alone restores the view: series, window, layout, norm and selected finding.

## Test cases

- **Core** (`residual::tests`):
  - `balance_residual_matches_sum`;
  - `pair_difference`;
  - `missing_member_reported`.
- **API** (`api/tests/db/test_compare_api.py`):
  - `chart_bins_identical_for_all_series`;
  - `chart_stats_from_raw_window` (mean, sd, min, max and first from raw usable samples,
    independent of `width_px`);
  - `chart_points_keep_bad_quality` (bad-quality finite samples appear in the points; a
    window of only bad samples has points and `stats` null);
  - `chart_rejects_count_and_unknown_ids`;
  - `residual_balance_band_and_episode`;
  - `residual_pair_no_band`;
  - `residual_missing_member` (a member without usable data in the window empties the
    residual and is listed);
  - `residual_skips_incomplete_bins` (a gap in one member leaves a gap in the residual, the
    other bins keep their values; bad-quality samples do not count).
- **Web** (Vitest, `web/src/__tests__/compare.*.test.ts`):
  - `axis_groups_by_unit` (1, 2 and 3 units);
  - `normalise_modes` (range with and without limits, z, index refusal on x₀ ≤ 0 and on a
    sign change, sd = 0 and hi = lo refusals, identical output at two widths, a series with
    null `stats` hidden with its reason);
  - `palette_contrast_light_dark`;
  - `url_state_round_trip`;
  - `finding_selection_marks_suspect`;
  - `table_view_rows`.

## Out of scope

- XY scatter of one series against another, coloured by time: story S10-7 (decided 2026-09-24).
- Compare periods (one series, several windows overlaid): later.
- A lag / cross-correlation explorer: later.
- More than 8 series (small multiples): with fleet baselines, S20.
- Saving a comparison as a named view, and sharing it outside the workspace: S12 sharing.
- Editing groups from the compare view: the groups page, S9-4.

## Implementation notes

Written on 15 Nov 2026, with the reasons for what differs from the first text (the sections
above are edited to match).

- **Usable means good or estimated.** The first text said "good or uncertain". The residual
  must count samples as `tby.balance_residual` does, and the core's `align` uses good and
  estimated samples (`Quality::is_usable`); `stats` follows the same rule, so a series
  normalises on the samples the checks judge.
- **The residual's band has one value per point.** A balance's band moves with the input, so
  `band` is two arrays beside `residual`, and each point carries the band of its own grid
  bin: a point drawn outside the band was outside it, a point inside was inside. Grid bins
  the downsampling drops are not drawn; M4 keeps each chart bin's lowest and highest
  residual, so an excursion can only be lost where the band itself moves inside one chart
  bin by more than the residual does. (A first version widened the band over the chart bin,
  which could hide an excursion; found in review.)
- **Non-finite numbers** never reach the JSON: residual and band values that overflow become
  null, and a series whose statistics are not finite gets `stats: null`.
- **The grid is the group's `grid` param**, so the lane shows the bins the check judged. The
  binding takes it as `auto` or a duration, as the checks' params do.
- **Bins that start before the window** (the grid is anchored at the epoch) are drawn at the
  window's start.
- **Row limit.** Both endpoints refuse a window whose series together exceed
  `TABAYYUN_CHART_MAX_ROWS` (422 `window_too_large`), like the series chart.
- **Tenancy.** Series ids come in the query, so the chart answers 404 with the ids the
  caller's workspace does not have; the residual route joined the cross-tenant route test.
- **Normalisation refuses per series.** A series a mode cannot show (hi = lo, sd = 0, an index
  base ≤ 0 or a sign change, no usable samples) is hidden with its reason in the legend, and
  the others are drawn.
- **`layout=lanes` with a normalised mode** keeps one lane per series, each on the
  dimensionless axis: the lanes are what the user asked for, the axis what the mode needs.
- **Keyboard.** Arrow keys step the cursor through the bins that hold data (Shift steps ten),
  plus and minus zoom, Home resets, Escape clears. The series page pans with the arrows; here
  the spec asks for a cursor.
- **Table.** The page shows at most 500 bins; the CSV holds all of them.
- **`finding=` without a window** reads the finding first and centres on it. Links built from
  a finding carry the window.
- **Palette.** Okabe-Ito, with orange `#c18600`, sky blue `#4a9bc8` and yellow `#9e962c` on
  the light background and `#666666` for black on the dark one, the smallest changes that
  reach 3:1. Series toggles are local state, not part of the URL.
- **A pair's lane** is labelled "A − B", in the group's member order.
- **Speed (criterion "8 × 1M under 1.5 s after the first load").** Measured in this container
  (4 cores) on 14 Nov 2026, 8 series of 1 000 000 points each in a local cache:
  - The API test answers the compare chart in 1 614 ms on the first call and 427 ms on the
    second. The series are read and downsampled in threads at the same time; one after the
    other the same request took about 1.2 s.
  - In Chromium against the built app and the local API, changing the window of the open
    page repaints in 821 to 1 497 ms (median 1 198 ms over five changes).
  - Opening the page cold, including the app shell, takes 1 254 to 1 701 ms (median 1 591 ms
    over six loads), so the first load does not meet the budget; the criterion asks for the
    loads after it.
- **Checked in Chromium** against the API with the `seed.py` data: the balance finding of
  `out-2` opens the four feeders with the residual lane, the suspect drawn thicker and
  labelled, the residual above the band over the finding's window, no console error and no
  failed request. The z-score mode and the table view open from the same URL.
