# Research 06 — Comparing several time series on one screen

Input for spec 018 (story S9-7, the compare view). Gathered 2026-09-23 with web research; every
claim links its source, and the last section lists what could not be verified. Statements
marked *(inference)* are ours, not the source's.

## 1. How established tools compare series

| Tool | Axes and lanes | Normalisation | Notes |
|---|---|---|---|
| Seeq | "Spread" gives each signal its own auto-scaled lane; "One Lane"; "One Y-Axis" gives selected signals a common min and max; axes are grouped by giving them the same letter ([lanes and axes](https://support.seeq.com/latest/cloud/adjusting-signal-lanes-axes-formatting)) | none documented | Chain View drops the time between conditions ([docs](https://support.seeq.com/latest/cloud/chain-view)); Capsule Time overlays segments on relative time and dims unselected capsules ([docs](https://support.seeq.com/latest/cloud/capsule-time)); no series limit, only "minimize the number of items" ([performance](https://support.seeq.com/R65/cloud/best-practices-for-seeq-performance)) |
| TrendMiner | Stacked view (one swim lane per tag, now the default) or Trend view (one lane); dragging one label onto another makes a group with a shared scale ([focus chart](https://userguide.trendminer.com/2024.R2.0/en/focus-chart.html), [TrendHub NG](https://userguide.trendminer.com/2025.R1.0/en/main-differences-in-trendhub-next-generation.html)) | none documented | Layers copy all tags onto other periods, aligned by start or end ([layers](https://userguide.trendminer.com/2025.R1.0/en/layer-creation.html)); layer comparison table with relative difference, KS similarity and Pearson ([docs](https://userguide.trendminer.com/en/layer-comparison.html)) |
| AVEVA PI Vision | `MultipleScales`: each trace scaled independently or all share one ([extensibility guide](https://github.com/AVEVA/AVEVA-Samples-PI-System/blob/main/docs/PI-Vision-Extensibility-Docs/PI%20Vision%20Extensibility%20Guide.md)) | none | Scale grouping for traces of the same unit arrived in PI Vision 2025 ([idea I-17](https://pisystem.feedback.aveva.com/ideas/PIVISION-I-17), [I-736](https://pisystem.feedback.aveva.com/ideas/PIVISION-I-736)) |
| Grafana (panel built on uPlot, [blog](https://grafana.com/blog/how-the-new-time-series-panel-brings-major-performance-improvements-and-new-visualization-features-to-grafana-7-4/)) | Axis placement per field override, "useful when comparing datasets with different units"; soft min/max; log and symlog ([docs](https://grafana.com/docs/grafana/latest/panels-visualizations/visualizations/time-series/)) | none built in | Legend series limit with "Show all" ([2026-03](https://grafana.com/whats-new/2026-03-15-legend-series-limit/)); shared crosshair and tooltip across panels ([dashboard settings](https://grafana.com/docs/grafana/latest/visualizations/dashboards/build-dashboards/modify-dashboard-settings/)) |
| Cognite Charts | Axes dragged to stack or overlay; "Merge units" joins axes of the same unit ([docs](https://docs.cognite.com/cdf/explore/charts)) | none | Calculations (differences), thresholds, event overlays, a timestamp slider reading all series |
| Timeseer.ai | not described publicly | — | Lists "broken correlations" among 100+ checks ([platform](https://www.timeseer.ai/platform)) |

Common pattern: none of these documents explicit 0–1, z-score or index-to-100 modes. Series
are compared on independent auto-scaled axes, on shared or unit-grouped axes, or in stacked
lanes. Units drive grouping (Cognite, PI Vision 2025). Events show as bands across the trend.

## 2. Comparison modes beyond overlay

- **Difference / residual trace:** Cognite calculations ([docs](https://docs.cognite.com/cdf/explore/charts)),
  Grafana "Add field from calculation" ([docs](https://grafana.com/docs/learning-paths/data-transformation/add-field-from-calculation/)).
- **XY scatter coloured by time:** Seeq colours by relative time by default ([XY plot](https://support.seeq.com/latest/cloud/xy-plot));
  TrendMiner colours blue to orange by time, limits multi-layer scatter to 5 tags ([scatter](https://userguide.trendminer.com/en/the-scatter-plot.html)).
- **Lag / cross-correlation:** TrendMiner searches upstream shifts up to 24 h ([TrendHub NG](https://userguide.trendminer.com/2025.R1.0/en/main-differences-in-trendhub-next-generation.html));
  Seeq's `seeq-correlation` add-on ([GitHub](https://github.com/seeq12/seeq-correlation)).
- **Compare periods:** TrendMiner layers, Seeq Capsule Time, uPlot's overlaid-periods demo
  ([demo](https://leeoniya.github.io/uPlot/demos/time-periods.html)).
- **Small multiples:** TrendMiner stacked lanes, Seeq "Spread",
  [Datawrapper](https://www.datawrapper.de/blog/small-multiple-line-charts).

## 3. uPlot

From the [README](https://github.com/leeoniya/uPlot) and [docs](https://github.com/leeoniya/uPlot/blob/master/docs/README.md):

- Performance: 166,650 points in 25 ms cold start, then about 100,000 points per ms. No
  series limit is given.
- Multiple y-axes and scales: each series names a `scale`, and each scale can have its own axis.
- Data is columnar with one shared x array. The docs warn this is awkward for series that
  cannot be aligned. `uPlot.join(tables, nullModes)` outer-joins per-series tables.
- `uPlot.sync(key)` with `cursor.sync` shares the cursor, series toggles and focus across
  charts. The sync demo also carries drag-to-zoom
  ([demo](https://leeoniya.github.io/uPlot/demos/sync-cursor.html)).
- Bands between two series are built in. Shaded time regions use draw hooks
  ([demo](https://leeoniya.github.io/uPlot/demos/draw-hooks.html)).
- The canvas has no data-table fallback: Grafana's open accessibility epic says so for its
  uPlot panel ([issue 118642](https://github.com/grafana/grafana/issues/118642)).

## 4. Downsampling several series for one chart

- **M4** ([Jugel et al., VLDB 2014](https://www.vldb.org/pvldb/vol7/p797-jugel.pdf)) keeps first,
  min, max and last per pixel column: at most 4·w original points, drawn without visible error.
  The paper covers one series.
- *(inference)* Running M4 with the same `(from, to, width_px)` on every series gives a shared
  pixel grid, while each series keeps its own timestamps. The client outer-joins them; for 8
  series at 2,000 px that is at most 64k rows, well inside uPlot's range.
- **MinMaxLTTB** ([Van Der Donckt et al. 2023](https://arxiv.org/abs/2305.00332);
  [tsdownsample](https://github.com/predict-idlab/tsdownsample)) returns indices into the
  original arrays. plotly-resampler downsamples each trace on its own
  ([GitHub](https://github.com/predict-idlab/plotly-resampler)).
- *(inference)* A residual such as A − B or Σin − Σout must be computed on raw, aligned data
  before downsampling: the M4 outputs of A and B do not share timestamps.

## 5. Colour and accessibility

- **How many categorical colours:**
  - three to five work best; at eight to ten matching becomes burdensome, so use direct labels
    ([Wilke](https://clauswilke.com/dataviz/color-pitfalls.html));
  - beyond 8 it is "close to impossible"
    ([Goedhart](https://thenode.biologists.com/data-visualization-with-flying-colors/research/)).
- **Okabe-Ito** ([source](https://jfly.uni-koeln.de/color/)): 8 colour-blind-safe colours,
  #E69F00 #56B4E9 #009E73 #F0E442 #0072B2 #D55E00 #CC79A7 #000000. The source advises pairing
  colour with line type or shape.
- *(inference, WCAG contrast formula)* Against white, orange (2.25), sky blue (2.31) and yellow
  (1.32) fall below 3:1. Against #121212, black fails (1.12). The palette needs light and dark
  variants.
- **IBM Carbon** fixes a 14-colour categorical order
  ([palettes](https://carbondesignsystem.com/data-visualization/color-palettes/)).
- **WCAG 2.2:**
  - 1.4.11: each line needs 3:1 against its background, lines need not contrast with each
    other, and the rule is waived when the data is also available as a table
    ([non-text contrast](https://www.w3.org/WAI/WCAG22/Understanding/non-text-contrast.html));
  - 1.4.1: colour must not be the only way to tell lines apart
    ([use of colour](https://www.w3.org/WAI/WCAG22/Understanding/use-of-color.html));
  - complex images need a short and a long description, with a data table as the long form
    ([WAI tutorial](https://www.w3.org/WAI/tutorials/images/complex/)).
- **Prior art in other libraries:** Highcharts (keyboard navigation, data table view,
  [docs](https://www.highcharts.com/docs/accessibility/accessibility-module)), ECharts `aria`
  (generated description, decal patterns,
  [docs](https://echarts.apache.org/handbook/en/best-practices/aria/)), Datawrapper (grey the
  context lines, colour the labels, dashes,
  [blog](https://www.datawrapper.de/blog/line-charts)).

## 6. Data-quality and reconciliation views

- **Data validation and reconciliation (VDI 2048):**
  - a measurement is suspect when its correction exceeds the 95 % confidence interval of its
    uncertainty;
  - overall quality is the weighted penalty sum over the χ² value and must stay below 1
    ([NEI article](https://www.neimagazine.com/advanced-reactorsfusion/how-data-validation-and-reconciliation-improves-nuclear-plant-performance-9329611/),
    [VDI 2048](https://www.vdi.de/en/home/vdi-standards/details/vdi-2048-blatt-1-control-and-quality-improvement-of-process-data-and-their-uncertainties-by-means-of-correction-calculation-for-operation-and-acceptance-tests)).
- **Products:**
  - BTB Jansky PROCESSPLUS shows flowsheets, trends and a suspected-tag display
    ([site](https://www.btbjansky.com/));
  - Belsim VALI lists "bad-actor instruments" ([site](https://belsim.com/vali-software/));
  - Sigmafine compares metered with reconciled flows to locate imbalances
    ([site](https://sigmafine.pimsoftinc.com/solutions/gas-transportation-and-distribution)).
- **Pattern:** measured against reconciled trends, per-tag penalties and a suspect list or
  flag. No public design shows which member of a redundant set is the outlier. This is room
  for Tabayyun, whose cross-series findings already name a suspect (specs 010, 011).

## Implications for spec 018

- **Series and axes:**
  - up to 8 series on one chart;
  - axes grouped by unit, with at most two visible y-axes;
  - three or more units fall back to stacked lanes, synced with `uPlot.sync`, zoom included.
- **Normalisation** (range, z-score, index = 100) goes beyond what the tools document. It is
  offered as an explicit mode with clear labels, and the readout always keeps raw values and
  units.
- **Residual lane** for balances and pairs, computed on the server from raw aligned data
  (spec 008's grid) before M4. Balances also show the loss band.
- **Findings:**
  - cross-series findings shade across all lanes;
  - the named suspect is drawn stronger and labelled, and the others are muted.
- **Colour and access:**
  - Okabe-Ito order with light and dark variants that meet 3:1;
  - dashes from the fifth series on;
  - direct end labels;
  - a "view as table" alternative with keyboard stepping.
- **Deferred to later stories:** scatter (XY coloured by time), compare periods,
  lag / cross-correlation and small multiples beyond 8.

## Could not verify

- docs.aveva.com PI Vision pages render client-side; Tableau's colour blog returned 403;
  belsim.com returned 503.
- The `echarts.connect` API page did not render.
- TrendMiner's "up to 20 layers" and per-tag time-shift wording came from search snippets
  only.
- Whether Grafana's per-query time shift is native.
- A Seeq formula page for differences.
- How Timeseer or Cognite visualise the outlier of a redundant set.
- M4 across several series and timestamp alignment are our extrapolation, not the paper's.
