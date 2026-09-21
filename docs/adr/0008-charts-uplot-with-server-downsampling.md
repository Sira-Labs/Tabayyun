# ADR-0008: uPlot for time series with server-side M4/MinMaxLTTB downsampling

- **Status:** Accepted
- **Date:** 2026-09-21

## Context
Series views must show years of 1 s data with findings overlaid, on desktop and phones,
without freezing the browser.

## Decision
The API never returns more than ~4× the pixel width of points per series: the Rust core
downsamples with M4 (preserves min/max/first/last per bucket, so spikes and flatlines stay
visible) for line charts and MinMaxLTTB for smoother views; zoom triggers a re-query. uPlot
renders time series; ECharts renders bars, heatmaps and distributions.

## Alternatives considered
| Option | Pros | Cons | Why not |
|---|---|---|---|
| Plotly | Rich | 3.6 MB bundle, heavy heap | Dashboard feel and mobile |
| ECharts for everything | One library | Slower than uPlot for pure time series at scale | Keep for non-TS charts |
| Recharts / visx | React-native API | SVG, unusable above ~10k points | Scale |
| Commercial WebGL charts | Millions of points | Licence cost | Not needed with downsampling |

## Consequences
Chart endpoints take `(series, from, to, width_px)`; caching keyed on those parameters.
