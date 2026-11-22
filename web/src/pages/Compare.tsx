import { keepPreviousData, useQueries, useQuery, type UseQueryResult } from "@tanstack/react-query";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { Suspense, lazy, useEffect, useMemo, useState, type KeyboardEvent } from "react";
import { ApiError } from "../api";
import { compareApi, hasResidual, orderByRole, unknownIdsOf, type CompareChartResponse } from "../compare/api";
import { axisLanes } from "../compare/axes";
import { emphasisOf, suspectOf } from "../compare/findings";
import { normalise } from "../compare/normalise";
import { defaultPair } from "../compare/scatter";
import { buildLanes, isDrawn, residualLane } from "../compare/plot";
import { binIndex, binWindow, binsOf, readoutAt, stepBin, tableRows } from "../compare/table";
import { useTheme } from "../compare/theme";
import { MAX_SERIES, MIN_SERIES, UUID_RE, searchOfState, stateOfSearch, validateCompareSearch, type CompareState } from "../compare/url";
import { formatLocalTime } from "../format";
import { aroundFinding, atLeastMin, chartApi, zoom, type Range } from "../seriesChart";
import type { Finding, Page } from "../types";
import { ErrorLine, linkButtonClass, panelClass } from "./admin/ui";
import { Controls } from "./compare/Controls";
import { CompareFindings } from "./compare/Findings";
import { Legend } from "./compare/Legend";
import { Readout, readoutText } from "./compare/Readout";
import { Scatter } from "./compare/Scatter";
import { SeriesPicker } from "./compare/SeriesPicker";
import { CompareTable } from "./compare/Table";
import { useRequestWidth } from "./SeriesDetail";

// uPlot loads with the page, not with the app (as on the series page).
const loadChart = () => import("../components/chart/CompareChart");
const CompareChart = lazy(loadChart);

// How many windows "Back" remembers.
const BACK_DEPTH = 50;
// Bins a Shift+arrow key steps.
const BIG_STEP = 10;

/** `A, B and C`. */
function listText(names: string[]): string {
  return names.length <= 1 ? names.join("") : `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`;
}

/** The findings of all series as one list, each once, earliest first. A stable function, so
 * `useQueries` keeps the combined result while the answers do not change. */
function combineFindings(results: UseQueryResult<Page<Finding>>[]) {
  const byId = new Map<string, Finding>();
  for (const r of results) for (const f of r.data?.items ?? []) byId.set(f.id, f);
  return {
    items: [...byId.values()].sort((a, b) => a.window.start - b.window.start),
    loading: results.some((r) => r.isPending),
    error: results.find((r) => r.error)?.error ?? null,
  };
}

/** `/compare`: 2 to 8 series on a shared time axis, the view's whole state in the URL (spec 018). */
export function Compare() {
  // The router merges the root's raw search into this route's: keep the known keys only.
  const state = stateOfSearch(validateCompareSearch(useSearch({ from: "/_app/compare" }) as Record<string, unknown>));
  const navigate = useNavigate({ from: "/compare" });
  const apply = (next: CompareState) => void navigate({ search: searchOfState(next) });
  const malformed = state.series.filter((id) => !UUID_RE.test(id));
  const problem =
    malformed.length > 0
      ? null
      : state.series.length < MIN_SERIES
        ? `Choose at least ${MIN_SERIES} series to compare; ${state.series.length === 0 ? "none is" : "one is"} chosen.`
        : state.series.length > MAX_SERIES
          ? `At most ${MAX_SERIES} series can be compared; ${state.series.length} are chosen. Remove some.`
          : null;
  if (malformed.length > 0 || problem) return <Setup state={state} problem={problem} unknown={malformed} onApply={apply} />;
  return <CompareView key={state.series.join(",")} state={state} onApply={apply} />;
}

type SetupProps = { state: CompareState; problem: string | null; unknown: string[]; onApply: (next: CompareState) => void };

/** What is wrong with the chosen series, and a picker to put it right. */
function Setup({ state, problem, unknown, onApply }: SetupProps) {
  const known = state.series.filter((id) => !unknown.includes(id));
  return (
    <section aria-labelledby="compare-heading" className="space-y-4">
      <h1 id="compare-heading" className="text-lg font-semibold">
        Compare series
      </h1>
      <div role="alert" className="space-y-1 text-sm text-amber-800 dark:text-amber-300">
        {problem && <p>{problem}</p>}
        {unknown.length > 0 && (
          <>
            <p>These series are unknown:</p>
            <ul className="list-disc pl-6 font-mono text-xs">
              {unknown.map((id) => (
                <li key={id}>{id}</li>
              ))}
            </ul>
          </>
        )}
      </div>
      <div className={panelClass}>
        <SeriesPicker
          key={known.join(",")}
          initial={known}
          min={MIN_SERIES}
          max={MAX_SERIES}
          submitLabel="Compare"
          onSubmit={(ids) => onApply({ ...state, series: ids })}
        />
      </div>
    </section>
  );
}

type Cursor = { chart: CompareChartResponse; bin: number; ts: number; byKey: boolean };

/** The comparison of a valid set of series. */
function CompareView({ state, onApply }: { state: CompareState; onApply: (next: CompareState) => void }) {
  const { ref: sizer, width } = useRequestWidth();
  const theme = useTheme();
  useEffect(() => {
    void loadChart();
  }, []);

  const group = useQuery({ queryKey: ["series-groups", state.group], queryFn: () => compareApi.group(state.group!), enabled: !!state.group });
  // A finding named without a window: the window is centred on it once it is known.
  const picked = useQuery({ queryKey: ["findings", state.finding], queryFn: () => compareApi.finding(state.finding!), enabled: !!state.finding });
  const waitsForFinding = !!state.finding && !state.range && picked.isPending;
  const requestRange = state.range ?? (picked.data ? aroundFinding(picked.data.window) : null);

  const chart = useQuery({
    queryKey: ["compare", "chart", state.series, requestRange?.from, requestRange?.to, width],
    queryFn: () => compareApi.chart(state.series, requestRange, width!),
    enabled: width !== null && !waitsForFinding,
    placeholderData: keepPreviousData,
    // A wrong request (count, unknown ids, window too large) does not get better by asking again.
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 1,
  });
  // The last chart that arrived stays on screen while the next loads or when it fails.
  const [shown, setShown] = useState<CompareChartResponse | null>(null);
  if (chart.data && chart.data !== shown) setShown(chart.data);
  const bins = useMemo(() => (shown ? binsOf(shown) : null), [shown]);
  const range = useMemo<Range | null>(() => (bins ? { from: bins.from, to: bins.to } : null), [bins]);
  // Before a chart has loaded (or when the requested window is refused), the controls show the
  // window asked for, so it can still be changed.
  const controlRange = range ?? requestRange;

  const findings = useQueries({
    queries: state.series.map((id) => ({
      queryKey: ["series", id, "findings", range?.from, range?.to],
      queryFn: () => chartApi.findings(id, range!),
      enabled: range !== null,
      placeholderData: keepPreviousData,
    })),
    combine: combineFindings,
  });
  const withResidual = !!group.data && hasResidual(group.data);
  const residual = useQuery({
    queryKey: ["series-groups", state.group, "residual", range?.from, range?.to, width],
    queryFn: () => compareApi.residual(state.group!, range!, width!),
    enabled: withResidual && range !== null && width !== null,
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 1,
  });

  // With a group, series are ordered by role: inputs, outputs, other members.
  const display = useMemo(() => {
    if (!shown) return [];
    const byId = new Map(shown.series.map((s) => [s.id, s]));
    return orderByRole([...byId.keys()], group.data).map((id) => byId.get(id)!);
  }, [shown, group.data]);
  const items = findings.items;
  const selected = state.finding ? (items.find((f) => f.id === state.finding) ?? picked.data) : undefined;
  const residualData = withResidual ? residual.data : undefined;

  const model = useMemo(() => {
    if (!range) return null;
    const norms = Object.fromEntries(display.map((s) => [s.id, normalise(state.norm, s)]));
    const specs = axisLanes(
      display.filter((s) => isDrawn(s, norms[s.id])),
      state.layout,
      state.norm,
    );
    const emphasis = emphasisOf(
      selected,
      display.map((s) => s.id),
    );
    const base = { findings: items, selected: selected?.id, range, theme };
    const lanes = buildLanes({ series: display, specs, norms, emphasis, ...base });
    const extra = residualData ? residualLane(residualData, base) : null;
    return { norms, emphasis, lanes: extra ? [...lanes, extra] : lanes };
  }, [display, state.norm, state.layout, selected, items, range, theme, residualData]);
  const rows = useMemo(() => (bins ? tableRows(display, bins) : []), [display, bins]);
  // The scatter marks the selected finding's points even when the window's list does not hold it.
  const marking = useMemo(() => (selected && !items.includes(selected) ? [...items, selected] : items), [items, selected]);

  const [hidden, setHidden] = useState<ReadonlySet<string>>(new Set());
  const toggle = (id: string) =>
    setHidden((prev) => {
      const next = new Set(prev);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  const [tableOpen, setTableOpen] = useState(false);
  const [changing, setChanging] = useState(false);
  const [stored, setCursor] = useState<Cursor | null>(null);
  // A cursor belongs to the chart it was set on.
  const cursor = stored && stored.chart === shown ? stored : null;

  const [backStack, setBackStack] = useState<Range[]>([]);
  const go = (next: Range, finding?: string) => {
    if (range) setBackStack((s) => [...s.slice(-(BACK_DEPTH - 1)), range]);
    onApply({ ...state, range: atLeastMin(next), finding });
  };
  const back = () => {
    const previous = backStack.at(-1);
    if (!previous) return;
    setBackStack((s) => s.slice(0, -1));
    onApply({ ...state, range: previous });
  };
  const reset = () => {
    if (range) setBackStack((s) => [...s.slice(-(BACK_DEPTH - 1)), range]);
    onApply({ ...state, range: null });
  };

  const onCursor = (ts: number | null) => {
    if (ts === null || !bins || !shown) return setCursor((prev) => (prev?.byKey ? prev : null));
    const bin = binIndex(ts, bins);
    setCursor((prev) => (prev && prev.chart === shown && prev.bin === bin && !prev.byKey ? prev : { chart: shown, bin, ts, byKey: false }));
  };
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!bins || !range || !shown) return;
    const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (step !== 0) {
      e.preventDefault();
      const bin = stepBin(rows, cursor?.bin ?? null, step * (e.shiftKey ? BIG_STEP : 1));
      if (bin === null) return;
      const window = binWindow(bin, bins);
      return setCursor({ chart: shown, bin, ts: (window.from + window.to) / 2, byKey: true });
    }
    if (e.key === "Escape") return setCursor(null);
    const next = e.key === "+" || e.key === "=" ? zoom(range, 0.5) : e.key === "-" || e.key === "_" ? zoom(range, 2) : null;
    if (next) {
      e.preventDefault();
      go(next, state.finding);
    } else if (e.key === "Home") {
      e.preventDefault();
      reset();
    }
  };

  const failure = chart.error instanceof ApiError ? chart.error : null;
  if (failure && failure.status === 404 && !chart.data) {
    const unknown = unknownIdsOf(failure.body, state.series);
    if (unknown.length > 0) return <Setup state={state} problem={null} unknown={unknown} onApply={onApply} />;
  }

  const nameOf = (id: string) => display.find((s) => s.id === id)?.name ?? group.data?.members.find((m) => m.series_id === id)?.external_id ?? id;
  const readings = cursor && bins ? readoutAt(display, bins, cursor.bin, cursor.ts) : null;
  const names = display.map((s) => s.name);
  const figureLabel = range
    ? `Comparison of ${listText(names)}, ${formatLocalTime(range.from)} to ${formatLocalTime(range.to)}. ${
        selected ? `Selected finding: ${selected.summary}.` : "No finding selected."
      } Drag to zoom, double-click to go back; arrow keys step the cursor through the bins, plus and minus zoom, Home resets the window.`
    : "Comparison";
  const refused = model ? display.filter((s) => s.ts.length > 0 && model.norms[s.id]?.ok === false) : [];

  return (
    <section aria-labelledby="compare-heading" className="space-y-3">
      <header className="flex flex-wrap items-center justify-between gap-2">
        <h1 id="compare-heading" className="text-lg font-semibold">
          Compare {names.length > 0 ? listText(names) : "series"}
          {group.data ? ` (${group.data.name})` : ""}
        </h1>
        <button type="button" className={linkButtonClass} aria-expanded={changing} onClick={() => setChanging((v) => !v)}>
          Change series
        </button>
      </header>
      {changing && (
        <div className={panelClass}>
          <SeriesPicker
            initial={state.series}
            min={MIN_SERIES}
            max={MAX_SERIES}
            submitLabel="Compare"
            onSubmit={(ids) => onApply({ ...state, series: ids, group: undefined, finding: undefined })}
          />
        </div>
      )}
      {controlRange && (
        <Controls
          range={controlRange}
          layout={state.layout}
          norm={state.norm}
          onRange={(r) => go(r, state.finding)}
          onReset={reset}
          onBack={back}
          canGoBack={backStack.length > 0}
          onLayout={(layout) => onApply({ ...state, layout })}
          onNorm={(norm) => onApply({ ...state, norm })}
          scatter={!!state.xy}
          onScatter={() => onApply({ ...state, xy: state.xy ? undefined : defaultPair(selected, display.length > 0 ? display.map((s) => s.id) : state.series) })}
        />
      )}
      {state.norm === "z" && <p className="text-sm text-slate-600 dark:text-slate-400">z-scores use the mean and spread of the visible window, so they change when you zoom.</p>}
      {refused.length > 0 && (
        <ul className="text-sm text-amber-800 dark:text-amber-300">
          {refused.map((s) => {
            const norm = model?.norms[s.id];
            return <li key={s.id}>{`${s.name}: ${norm && !norm.ok ? norm.reason : ""}`}</li>;
          })}
        </ul>
      )}
      <ErrorLine error={chart.error} prefix="Could not load the chart" />
      <ErrorLine error={group.error} prefix="Could not load the series group" />
      {selected && (
        <p role="status" className="rounded border border-sky-300 bg-sky-50 px-3 py-2 text-sm dark:border-sky-800 dark:bg-sky-950">
          <span className="font-medium">Selected finding:</span> {selected.summary}{" "}
          <span className="text-slate-600 dark:text-slate-400">(suspect: {nameOf(suspectOf(selected))})</span>{" "}
          <button type="button" className={linkButtonClass} onClick={() => onApply({ ...state, finding: undefined })}>
            Clear selection
          </button>
        </p>
      )}
      <div ref={sizer} className={`${panelClass} min-h-24`} aria-busy={chart.isFetching}>
        {shown && range && model ? (
          <>
            {model.lanes.length > 0 ? (
              <Suspense fallback={<p className="text-sm text-slate-600 dark:text-slate-400">Loading the chart…</p>}>
                <CompareChart
                  lanes={model.lanes}
                  range={range}
                  theme={theme}
                  hidden={hidden}
                  keyCursor={cursor?.byKey ? cursor.ts : null}
                  label={figureLabel}
                  onSelect={(r) => go(r, state.finding)}
                  onBack={back}
                  onCursor={onCursor}
                  onKeyDown={onKeyDown}
                />
              </Suspense>
            ) : (
              <p className="text-sm">Nothing to draw in this window.</p>
            )}
            {residualData && residualData.missing.length > 0 && (
              <p className="mt-2 text-sm text-amber-800 dark:text-amber-300">
                No residual: {listText(residualData.missing.map(nameOf))} {residualData.missing.length === 1 ? "has" : "have"} no usable samples in this window.
              </p>
            )}
            {withResidual && !(residual.error instanceof ApiError && residual.error.status === 422) && (
              <ErrorLine error={residual.error} prefix="Could not load the residual" />
            )}
            <Legend series={display} norms={model.norms} emphasis={model.emphasis} theme={theme} hidden={hidden} onToggle={toggle} />
            <Readout series={display} readings={readings} at={cursor?.ts ?? null} theme={theme} />
            <p aria-live="polite" className="sr-only" data-testid="compare-live">
              {cursor?.byKey && readings ? readoutText(display, readings, cursor.ts) : ""}
            </p>
          </>
        ) : shown ? (
          <p className="text-sm">No data is cached for these series yet.</p>
        ) : chart.error ? null : (
          <p className="text-sm text-slate-600 dark:text-slate-400">Loading the chart…</p>
        )}
      </div>
      {state.xy && range && (
        <div className={panelClass}>
          <Scatter series={display} xy={state.xy} range={range} findings={marking} selected={selected?.id} theme={theme} onXy={(xy) => onApply({ ...state, xy })} />
        </div>
      )}
      {shown && range && (
        <div className={panelClass}>
          <button type="button" className={linkButtonClass} aria-expanded={tableOpen} onClick={() => setTableOpen((v) => !v)}>
            View as table
          </button>
          {tableOpen && <CompareTable series={display} rows={rows} findings={items} />}
        </div>
      )}
      <ErrorLine error={findings.error} prefix="Could not load the findings" />
      {range && (
        <CompareFindings
          findings={items}
          selected={selected?.id}
          loading={findings.loading}
          nameOf={nameOf}
          onPick={(f) => go(aroundFinding(f.window), f.id)}
        />
      )}
    </section>
  );
}
