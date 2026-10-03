# Spec 020 — Run report overview: series facts and a findings timeline

Side story requested by the owner on 3 Oct 2026, after loading the public examples (spec 019):
"the table alone makes it hard to get an overview". It depends on spec 005 (the run report),
spec 004 (series metadata) and spec 019 (`metadata.example`). Packages: `web/src/` only.

## Goal

The run report answers three questions before the table does: what this series is, how much
time it covers, and where in that time the findings are.

- **Header:** the data window also gives its span in words and the sampling step, for example
  "2.0 years · hourly". The sample count keeps its own row.
- **Timeline:** one lane per check, spanning the run's data window, with a mark at each
  finding's window, coloured by severity. The axis is labelled in years, months, days or hours,
  whichever fits the span. Selecting a mark opens that finding in the table below and scrolls
  to it.
- **Series card:** it also shows the sampling interval and, when the series came from
  `public.py`, what it is: source, licence, link and notes.

The values themselves (a chart of the series) remain story S9-5, which needs the downsampled
series endpoint.

## User story

As the owner, I open the run of `jena-wind-speed` and see at once that it covers 8 years of
10-minute data. The drift findings cluster in a few months, and the one physical-range finding
sits in 2016. I click that mark to read its evidence.

## Interface

No API change. The web app reads fields the API already returns:

- `Series.expected_interval_ns`;
- `Series.metadata.example` (`source`, `url`, `licence`, `notes`);
- the run's `window` and `stats.n_samples`;
- each finding's `window`, `check_id`, `severity`, `summary` and `series_id`.

New pure helpers in `web/src/timeline.ts`:

- `formatSpan(ns)` gives "2.0 years", "11 months", "14 days", "6 hours" or "45 minutes".
- `formatStep(ns)` gives "every second", "every 10 minutes", "hourly", "daily", or
  "every 2.5 hours".
- `axisTicks(start, end)` gives 3–12 ticks at whole years, quarters, months, days or hours.
  Each tick has its position (0 to 1) and a label.
- `lanes(findings)` groups findings into lanes by check, ordered by each lane's
  most severe finding, then check id. On a dataset run each lane is one check and one series, so
  its label names the series.

## Behaviour

1. **Header.** When the run has a window, the line after it reads `<span> · <step>`.
   - The step is `expected_interval_ns` when the series declares one.
   - Otherwise it is estimated as `(end − start) / (n_samples − 1)` and prefixed with "≈".
   - The step is left out with fewer than 2 samples, and on a dataset run, whose series may
     differ in step.
2. **Timeline.** It is shown when the run succeeded, has a window and has at least one finding.
   - Lanes are ordered by their most severe finding, then by check id.
   - A mark spans `max(start, window.start)` to `min(end, window.end)`, at least 3 px wide so
     a short finding stays visible.
   - Each mark is a button whose accessible name is `<severity> <check>: <summary>, <start>,
     <duration>`.
   - Activating a mark opens that finding's row (`aria-expanded=true`), scrolls it into view
     and moves focus to it.
   - With more than 12 lanes, the first 12 are shown with a "Show all N checks" toggle.
3. **Series card.** It adds "Sampling" (the declared interval, or "not set").
   - With `metadata.example`, it adds "Source" (a link to `url`), "Licence" and "Notes".
   - Links open in a new tab with `rel="noreferrer"`.
4. **Text alternative.** The table remains the complete, accessible list. The timeline adds
   no information the table lacks, only position in time.

## Acceptance criteria

- [x] Header shows span and step; the step is estimated with "≈" when not declared.
- [x] Timeline lanes per check with severity-coloured marks on a year, month, day or hour axis;
      a mark opens and focuses its row.
- [x] Series card shows the sampling interval and the `public.py` source, licence, link and
      notes.
- [x] Unit tests for `formatSpan`, `formatStep`, `axisTicks` and `lanes`; a RunReport test
      clicks a mark and finds its row expanded.
- [x] `pnpm lint`, `pnpm build` and `pnpm test` pass.

## Test cases

- **`timeline.test.ts`:**
  - span and step wording at each boundary;
  - ticks for 2 hours, 3 days, 4 months, 2 years and 8 years, all between 3 and 12, in order,
    and inside 0 to 1;
  - lanes ordered by severity, then check, with dataset-run labels.
- **`RunReport.test.tsx`:**
  - the header line "2.0 years · hourly" (declared) and "≈ every 10 minutes" (estimated);
  - the timeline has a lane per check, and clicking a mark expands its row;
  - the series card shows the source link and licence from `metadata.example`.

## Out of scope

- The value chart, quality rug and baseline band: S9-5.
- Zoom and brushing on the timeline: S9-5, which shares the uPlot cursor.
- Several series on one chart: spec 018 (S9-7).
