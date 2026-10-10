import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { Suspense, lazy, useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, api } from "../api";
import { SeverityBadge } from "../components/Badges";
import { buttonClass } from "../components/Brand";
import { RUG_CLASS } from "../components/chart/rug";
import { formatDuration, formatLocalTime, formatNumber } from "../format";
import {
  PRESETS,
  aroundFinding,
  atLeastMin,
  chartApi,
  findingRects,
  presetRange,
  rangeOfSearch,
  requestWidth,
  searchOfRange,
  validateSeriesPageSearch,
  type Band,
  type ChartResponse,
  type ProfileResponse,
  type Range,
} from "../seriesChart";
import { sourcesApi } from "../sources";
import { formatStep } from "../timeline";
import type { Finding, Series } from "../types";
import { ErrorLine, inputClass, panelClass } from "./admin/ui";

// uPlot loads with the first chart, not with the app.
const SeriesChart = lazy(() => import("../components/chart/SeriesChart"));

const NS_PER_MS = 1e6;
// How many ranges "Back" remembers.
const BACK_DEPTH = 50;

/** A datetime-local value (local time, minutes) of an ns instant. */
function localInput(ns: number): string {
  const d = new Date(ns / NS_PER_MS);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** The ns instant of a datetime-local value, or NaN. */
function nsOfInput(value: string): number {
  return Date.parse(value) * NS_PER_MS;
}

function rangeOf(window: { from: string; to: string } | { start: string; end: string } | null): Range | null {
  if (!window) return null;
  const [from, to] = "from" in window ? [window.from, window.to] : [window.start, window.end];
  return { from: Number(from), to: Number(to) };
}

/** The request width, measured from the element; null until the first measurement. */
function useRequestWidth() {
  const ref = useRef<HTMLDivElement>(null);
  // Without ResizeObserver (tests, old browsers) the chart asks for 1200 px.
  const [width, setWidth] = useState<number | null>(typeof ResizeObserver === "undefined" ? 1200 : null);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => setWidth(requestWidth(el.clientWidth)));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return { ref, width };
}

/** `/series/$seriesId`: values with findings, quality, band and limits; profile and metadata (spec 025). */
export function SeriesDetail() {
  const { seriesId } = useParams({ from: "/_app/series/$seriesId" });
  // The router merges the root's raw search into this route's: keep the known keys only.
  const search = validateSeriesPageSearch(useSearch({ from: "/_app/series/$seriesId" }) as Record<string, unknown>);
  const navigate = useNavigate({ from: "/series/$seriesId" });
  const urlRange = rangeOfSearch(search);
  const { ref: sizer, width } = useRequestWidth();

  const series = useQuery({ queryKey: ["series", seriesId], queryFn: () => api.getSeries(seriesId) });
  const sourceId = series.data?.source_id;
  const source = useQuery({
    queryKey: ["sources", sourceId],
    queryFn: () => sourcesApi.get(sourceId!),
    enabled: !!sourceId,
  });
  const chart = useQuery({
    queryKey: ["series", seriesId, "chart", urlRange?.from, urlRange?.to, width],
    queryFn: () => chartApi.chart(seriesId, urlRange, width!),
    enabled: width !== null,
    placeholderData: keepPreviousData,
  });
  const profile = useQuery({ queryKey: ["series", seriesId, "profile"], queryFn: () => chartApi.profile(seriesId) });

  // The last chart that arrived stays on screen while the next loads or when it fails.
  const [shown, setShown] = useState<ChartResponse | null>(null);
  if (chart.data && chart.data !== shown) setShown(chart.data);
  const range = rangeOf(shown?.window ?? null);
  const extent = rangeOf(shown?.extent ?? null);

  const findings = useQuery({
    queryKey: ["series", seriesId, "findings", range?.from, range?.to],
    queryFn: () => chartApi.findings(seriesId, range!),
    enabled: range !== null,
    placeholderData: keepPreviousData,
  });

  const [backStack, setBackStack] = useState<Range[]>([]);
  const go = (next: Range | "all", finding?: string) => {
    const target = next === "all" ? extent : atLeastMin(next);
    if (!target) return;
    if (range) setBackStack((s) => [...s.slice(-(BACK_DEPTH - 1)), range]);
    void navigate({ search: { ...searchOfRange(target), ...(finding ? { finding } : {}) } });
  };
  const back = () => {
    const previous = backStack.at(-1);
    if (!previous) return;
    setBackStack((s) => s.slice(0, -1));
    void navigate({ search: searchOfRange(previous) });
  };

  const tooLarge = chart.error instanceof ApiError && chart.error.status === 422 && chart.error.message === "window_too_large";
  const items = findings.data?.items ?? [];
  const selected = items.some((f) => f.id === search.finding) ? search.finding : undefined;

  useEffect(() => {
    if (selected) document.getElementById(`finding-${selected}`)?.scrollIntoView?.({ block: "nearest" });
  }, [selected]);

  if (series.error instanceof ApiError && series.error.status === 404) return <p className="p-6">Series not found.</p>;
  const s = series.data;
  const band = profile.data?.band ?? null;

  return (
    <section aria-labelledby="series-heading" className="space-y-4">
      <SeriesHeader series={s} sourceName={source.data?.name} />
      <ErrorLine error={series.error} prefix="Could not load the series" />
      <div className="grid gap-4 lg:grid-cols-[1fr_20rem]">
        <div className="min-w-0 space-y-3">
          {range && extent && <RangeBar range={range} extent={extent} onRange={go} onBack={back} canGoBack={backStack.length > 0} />}
          {tooLarge && (
            <p role="alert" className="text-sm text-amber-800 dark:text-amber-300">
              This range holds too many samples to draw. Zoom in or pick a shorter range.
            </p>
          )}
          {!tooLarge && <ErrorLine error={chart.error} prefix="Could not load the chart" />}
          <div ref={sizer} className={`${panelClass} min-h-24`} aria-busy={chart.isFetching}>
            {shown && range && extent && s ? (
              <>
                <Suspense fallback={<p className="text-sm text-slate-600 dark:text-slate-400">Loading the chart…</p>}>
                  <SeriesChart
                    chart={shown}
                    range={range}
                    label={s.name}
                    unit={s.unit}
                    overlay={{
                      rects: findingRects(items, range),
                      selected,
                      band,
                      limits: { min: s.physical_min, max: s.physical_max },
                    }}
                    onSelect={(r) => go(r)}
                    onBack={back}
                    onKey={(next) => go(next)}
                  />
                </Suspense>
                <ChartLegend chart={shown} band={band} series={s} />
              </>
            ) : shown && !shown.extent ? (
              <EmptyState sourceId={sourceId} sourceType={source.data?.type} />
            ) : (
              <p className="text-sm text-slate-600 dark:text-slate-400">Loading the chart…</p>
            )}
          </div>
          {range && (
            <FindingsList
              findings={items}
              selected={selected}
              loading={findings.isPending}
              onPick={(f) => go(aroundFinding(f.window), f.id)}
            />
          )}
        </div>
        <aside className="space-y-4">
          {s && <MetadataPanel series={s} />}
          <ProfilePanel profile={profile.data} error={profile.error} />
        </aside>
      </div>
    </section>
  );
}

function SeriesHeader({ series, sourceName }: { series: Series | undefined; sourceName: string | undefined }) {
  return (
    <header className="space-y-1">
      <p className="text-sm">
        <Link to="/series" className="text-sky-800 underline underline-offset-2 dark:text-sky-300">
          Series
        </Link>
      </p>
      <h1 id="series-heading" className="text-lg font-semibold">
        {series?.name ?? "Series"}
      </h1>
      {series && (
        <dl className="flex flex-wrap gap-x-6 gap-y-1 text-sm">
          <div className="flex gap-1">
            <dt className="text-slate-600 dark:text-slate-400">External id</dt>
            <dd className="break-all font-mono text-xs leading-5">{series.external_id}</dd>
          </div>
          <div className="flex gap-1">
            <dt className="text-slate-600 dark:text-slate-400">Source</dt>
            <dd>
              <Link
                to="/sources/$sourceId"
                params={{ sourceId: series.source_id }}
                className="text-sky-800 underline underline-offset-2 dark:text-sky-300"
              >
                {sourceName ?? "open"}
              </Link>
            </dd>
          </div>
          <div className="flex gap-1">
            <dt className="text-slate-600 dark:text-slate-400">Unit</dt>
            <dd>{series.unit ?? "—"}</dd>
          </div>
          <div className="flex gap-1">
            <dt className="text-slate-600 dark:text-slate-400">Kind</dt>
            <dd>{series.kind}</dd>
          </div>
          <div className="flex gap-1">
            <dt className="text-slate-600 dark:text-slate-400">Score</dt>
            <dd className="tabular-nums">{series.latest_score ? series.latest_score.overall.toFixed(1) : "—"}</dd>
          </div>
          <div className="flex gap-1">
            <dt className="text-slate-600 dark:text-slate-400">Open findings</dt>
            <dd className="tabular-nums">{series.open_findings}</dd>
          </div>
          <div className="flex gap-1">
            <dt className="text-slate-600 dark:text-slate-400">Last run</dt>
            <dd>{series.last_run_at ? formatLocalTime(series.last_run_at) : "—"}</dd>
          </div>
        </dl>
      )}
    </header>
  );
}

type RangeBarProps = {
  range: Range;
  extent: Range;
  onRange: (next: Range | "all") => void;
  onBack: () => void;
  canGoBack: boolean;
};

/** Presets, From and To inputs, and Back. */
function RangeBar({ range, extent, onRange, onBack, canGoBack }: RangeBarProps) {
  const [fromText, setFromText] = useState(localInput(range.from));
  const [toText, setToText] = useState(localInput(range.to));
  const [error, setError] = useState<string | null>(null);
  // Follow range changes made elsewhere (zoom, keys, back), keyed on the range shown.
  const [shownKey, setShownKey] = useState(`${range.from}-${range.to}`);
  const key = `${range.from}-${range.to}`;
  if (key !== shownKey) {
    setShownKey(key);
    setFromText(localInput(range.from));
    setToText(localInput(range.to));
    setError(null);
  }
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const from = nsOfInput(fromText);
    const to = nsOfInput(toText);
    if (!Number.isFinite(from) || !Number.isFinite(to)) return setError("Enter both From and To.");
    if (to <= from) return setError("To must be later than From.");
    setError(null);
    onRange({ from, to });
  };
  const chip = "rounded border border-slate-300 px-2 py-1 text-sm hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800";
  return (
    <form onSubmit={submit} aria-label="Range" className="flex flex-wrap items-end gap-2">
      <div role="group" aria-label="Presets" className="flex flex-wrap gap-1">
        {PRESETS.map((p) => (
          <button key={p.key} type="button" className={chip} onClick={() => onRange(presetRange(extent, p.span))}>
            {p.label}
          </button>
        ))}
        <button type="button" className={chip} onClick={() => onRange("all")}>
          All
        </button>
      </div>
      <label className="flex flex-col gap-1 text-sm">
        From
        <input type="datetime-local" className={inputClass} value={fromText} onChange={(e) => setFromText(e.target.value)} />
      </label>
      <label className="flex flex-col gap-1 text-sm">
        To
        <input type="datetime-local" className={inputClass} value={toText} onChange={(e) => setToText(e.target.value)} />
      </label>
      <button type="submit" className={buttonClass}>
        Show
      </button>
      <button type="button" className={chip} onClick={onBack} disabled={!canGoBack}>
        Back
      </button>
      {error && (
        <p role="alert" className="basis-full text-sm text-red-700 dark:text-red-400">
          {error}
        </p>
      )}
    </form>
  );
}

function ChartLegend({ chart, band, series }: { chart: ChartResponse; band: Band | null; series: Series }) {
  const limits = series.physical_min !== null || series.physical_max !== null;
  return (
    <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-600 dark:text-slate-400">
      <span>
        {formatNumber(chart.n_raw)} samples, {formatNumber(chart.ts.length)} drawn
      </span>
      {band && (
        <span>
          <span aria-hidden className="mr-1 inline-block h-2 w-3 bg-sky-500/30 align-middle" />
          Operating band {formatNumber(band.lo)} to {formatNumber(band.hi)} (
          {band.source === "metadata" ? "from the series' operational range" : "learned from the last 28 days"})
        </span>
      )}
      {limits && (
        <span>
          <span aria-hidden className="mr-1 inline-block w-3 border-t-2 border-dashed border-red-600 align-middle" />
          Physical limits
        </span>
      )}
      <span>
        Quality:{" "}
        {(["bad", "estimated", "uncertain"] as const).map((q) => (
          <span key={q} className="mr-2">
            <span aria-hidden className={`mr-1 inline-block h-2 w-3 align-middle ${RUG_CLASS[q]}`} />
            {q}
          </span>
        ))}
      </span>
    </div>
  );
}

function EmptyState({ sourceId, sourceType }: { sourceId: string | undefined; sourceType: string | undefined }) {
  return (
    <div className="space-y-1 text-sm">
      <p>No data is cached for this series yet.</p>
      {sourceId && sourceType && sourceType !== "upload" && (
        <p>
          Fetch a window on{" "}
          <Link to="/sources/$sourceId" params={{ sourceId }} className="text-sky-800 underline underline-offset-2 dark:text-sky-300">
            its source's page
          </Link>{" "}
          to see it here.
        </p>
      )}
    </div>
  );
}

type FindingsListProps = {
  findings: Finding[];
  selected: string | undefined;
  loading: boolean;
  onPick: (finding: Finding) => void;
};

function FindingsList({ findings, selected, loading, onPick }: FindingsListProps) {
  return (
    <section aria-labelledby="window-findings" className={panelClass}>
      <h2 id="window-findings" className="mb-2 text-base font-semibold">
        Findings in this range
      </h2>
      {loading ? (
        <p className="text-sm text-slate-600 dark:text-slate-400">Loading…</p>
      ) : findings.length === 0 ? (
        <p className="text-sm text-slate-600 dark:text-slate-400">No findings in this range.</p>
      ) : (
        <ul className="divide-y divide-slate-200 dark:divide-slate-800">
          {findings.map((f) => (
            <li key={f.id} id={`finding-${f.id}`}>
              <button
                type="button"
                aria-current={f.id === selected ? "true" : undefined}
                onClick={() => onPick(f)}
                className={`flex w-full flex-col gap-1 px-2 py-2 text-left text-sm hover:bg-slate-50 dark:hover:bg-slate-800 ${
                  f.id === selected ? "bg-sky-50 dark:bg-sky-950" : ""
                }`}
              >
                <span className="flex flex-wrap items-center gap-2">
                  <SeverityBadge severity={f.severity} />
                  <span className="font-mono text-xs">{f.check_id}</span>
                  <span className="text-xs text-slate-600 dark:text-slate-400">
                    {formatLocalTime(f.window.start)} · {formatDuration(f.window.end - f.window.start)}
                    {f.status !== "open" ? ` · ${f.status}` : ""}
                  </span>
                </span>
                <span>{f.summary}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="text-slate-600 dark:text-slate-400">{label}</dt>
      <dd>{children}</dd>
    </>
  );
}

const num = (v: number | null | undefined) => (v === null || v === undefined ? "—" : formatNumber(v));

function MetadataPanel({ series: s }: { series: Series }) {
  const range = (lo: number | null, hi: number | null) => (lo === null && hi === null ? "not set" : `${num(lo)} to ${num(hi)}`);
  const extra = Object.entries(s.metadata ?? {}).filter(([, v]) => v === null || ["string", "number", "boolean"].includes(typeof v));
  return (
    <section aria-labelledby="metadata-heading" className={`${panelClass} text-sm`}>
      <h2 id="metadata-heading" className="mb-2 text-base font-semibold">
        Metadata
      </h2>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1">
        <Row label="Sampling">{s.expected_interval_ns ? formatStep(s.expected_interval_ns) : "not set"}</Row>
        <Row label="Physical limits">{range(s.physical_min, s.physical_max)}</Row>
        <Row label="Operational range">{range(s.operational_min, s.operational_max)}</Row>
        <Row label="Resolution">{num(s.resolution)}</Row>
        <Row label="Never negative">{s.non_negative === null || s.non_negative === undefined ? "not set" : s.non_negative ? "yes" : "no"}</Row>
        <Row label="Asset path">{s.asset_path ?? "—"}</Row>
        {extra.map(([k, v]) => (
          <Row key={k} label={k}>
            {String(v)}
          </Row>
        ))}
      </dl>
    </section>
  );
}

function ProfilePanel({ profile, error }: { profile: ProfileResponse | undefined; error: unknown }) {
  const p = profile?.profile;
  const counts = profile?.quality_counts;
  const total = counts ? Object.values(counts).reduce((a, b) => a + b, 0) : 0;
  const window = rangeOf(profile?.window ?? null);
  return (
    <section aria-labelledby="profile-heading" className={`${panelClass} text-sm`}>
      <h2 id="profile-heading" className="mb-1 text-base font-semibold">
        Profile
      </h2>
      <ErrorLine error={error} prefix="Could not load the profile" />
      {window && (
        <p className="mb-2 text-xs text-slate-600 dark:text-slate-400">
          {formatLocalTime(window.from)} to {formatLocalTime(window.to)}, without bad-quality samples
        </p>
      )}
      {profile && !p && <p className="text-slate-600 dark:text-slate-400">No usable samples to profile.</p>}
      {p && (
        <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1">
          <Row label="Samples">{formatNumber(p.n_samples)}</Row>
          <Row label="Interval">{p.expected_interval_ns ? formatStep(p.expected_interval_ns) : "—"}</Row>
          <Row label="Median">{num(p.median)}</Row>
          <Row label="MAD">{num(p.mad)}</Row>
          <Row label="p0.1 to p99.9">
            {num(p.p001)} to {num(p.p999)}
          </Row>
          <Row label="Min to max">
            {num(p.min)} to {num(p.max)}
          </Row>
          <Row label="Resolution">{num(p.resolution)}</Row>
          <Row label="Noise σ">{num(p.noise_mad)}</Row>
          <Row label="Period">
            {p.dominant_period_ns ? `${formatDuration(p.dominant_period_ns)} (strength ${num(p.seasonal_strength)})` : "none found"}
          </Row>
        </dl>
      )}
      {counts && total > 0 && (
        <p className="mt-2 text-xs">
          Quality:{" "}
          {Object.entries(counts)
            .filter(([, n]) => n > 0)
            .map(([q, n]) => `${q} ${((n / total) * 100).toFixed(1)} %`)
            .join(", ")}
        </p>
      )}
    </section>
  );
}
