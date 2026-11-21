// The workspace overview (spec 032): types and API call of `GET /api/overview`, the page's URL
// search, and the pure helpers its tiles and charts are drawn from.
import { apiGet } from "./api";
import { UUID_RE } from "./compare/url";
import { DIMENSIONS, SEVERITIES } from "./findingsInbox";
import type { HealthStatus } from "./sources";
import type { Severity } from "./types";

export const WINDOWS = [7, 30, 90] as const;
export type WindowDays = (typeof WINDOWS)[number];
export const DEFAULT_DAYS: WindowDays = 30;
/** The tiles' metrics in the API's order: overall, then the seven dimensions. */
export const METRICS = ["overall", ...DIMENSIONS] as const;
export type OverviewMetric = (typeof METRICS)[number];
const MAX_ASSET = 200;

export type TrendPoint = { day: string; mean: number; n: number };

/** One score tile: `mean` and `min` over each series' newest day in the window. */
export type OverviewTile = {
  metric: OverviewMetric;
  mean: number | null;
  min: number | null;
  n_series: number;
  trend: TrendPoint[];
};

/** A series among the worst, with the metric whose value is lowest. */
export type WorstItem = {
  series_id: string;
  name: string;
  asset_path: string | null;
  overall: number | null;
  metric: OverviewMetric;
  value: number | null;
  open_findings: number;
  computed_at: string;
};

export type SeverityCounts = Record<Severity, number>;
export type FindingsDay = { day: string } & SeverityCounts;

export type OverviewSource = {
  id: string;
  name: string;
  type: string;
  enabled: boolean;
  status: HealthStatus;
  /** The spec shows a time; the source's stored health holds its last fetch as an object. */
  last_fetch: string | { finished_at?: string | null } | null;
  n_series: number;
};

/** `GET /api/overview`. */
export type Overview = {
  window: { days: number; start: string; end: string };
  series: { total: number; scored: number };
  tiles: OverviewTile[];
  worst: { share: number; n: number; items: WorstItem[] };
  findings: { open: SeverityCounts; days: FindingsDay[] };
  sources: OverviewSource[];
};

/** The page's URL search: `days` only when it is not the default. */
export type OverviewSearch = { days?: WindowDays; asset?: string; source?: string };

/** The overview's URL search: a known window other than 30, an asset prefix of at most 200
 * characters and a source id that is a UUID; anything else is dropped. */
export function validateOverviewSearch(raw: Record<string, unknown>): OverviewSearch {
  const out: OverviewSearch = {};
  const days = Number(raw.days);
  if ((WINDOWS as readonly number[]).includes(days) && days !== DEFAULT_DAYS) out.days = days as WindowDays;
  const asset = typeof raw.asset === "string" ? raw.asset.trim() : "";
  if (asset && asset.length <= MAX_ASSET) out.asset = asset;
  const source = typeof raw.source === "string" ? raw.source.trim() : "";
  if (source && UUID_RE.test(source)) out.source = source;
  return out;
}

/** The window a search names. */
export function daysOf(search: OverviewSearch): WindowDays {
  return search.days ?? DEFAULT_DAYS;
}

/** The request of a search: `days` always, the filters only when given. */
export function overviewUrl(search: OverviewSearch): string {
  const q = new URLSearchParams({ days: String(daysOf(search)) });
  if (search.asset) q.set("asset", search.asset);
  if (search.source) q.set("source", search.source);
  return `/api/overview?${q.toString()}`;
}

export const overviewApi = {
  get: (search: OverviewSearch) => apiGet<Overview>(overviewUrl(search)),
};

/** Query keys of the overview; all start with `"overview"`. */
export const overviewKeys = {
  all: ["overview"] as const,
  get: (search: OverviewSearch) => ["overview", overviewUrl(search)] as const,
};

// ---- words ----

/** A metric's name for people: `Overall`, `Completeness`, … */
export function metricLabel(metric: string): string {
  return metric.charAt(0).toUpperCase() + metric.slice(1);
}

/** A score with one decimal, or a dash without one. */
export function scoreText(value: number | null | undefined): string {
  return value === null || value === undefined || !Number.isFinite(value) ? "—" : value.toFixed(1);
}

/** The text alternative of a tile's sparkline: "from 97.1 to 96.4 over 30 days". */
export function trendWords(trend: readonly TrendPoint[], days: number): string {
  const first = trend[0];
  const last = trend.at(-1);
  if (!first || !last) return `no scores in the last ${days} days`;
  if (trend.length === 1) return `${scoreText(first.mean)} on ${first.day}, the only scored day in ${days} days`;
  return `from ${scoreText(first.mean)} to ${scoreText(last.mean)} over ${days} days`;
}

/** "50 of 498 shown" when the list holds fewer than the worst share, else nothing. */
export function shownWords(listed: number, n: number): string | null {
  return n > listed ? `${listed.toLocaleString("en-US")} of ${n.toLocaleString("en-US")} shown` : null;
}

/** All of a day's findings. */
export function dayTotal(day: SeverityCounts): number {
  return SEVERITIES.reduce((sum, s) => sum + (day[s] ?? 0), 0);
}

/** A day's findings in words, most severe first: "2026-11-20: 1 critical, 4 high". */
export function dayWords(day: FindingsDay): string {
  const parts = SEVERITIES.filter((s) => day[s] > 0).map((s) => `${day[s]} ${s}`);
  return `${day.day}: ${parts.length ? parts.join(", ") : "none"}`;
}

/** The text alternative of the findings chart: the window, the total and the busiest day. */
export function findingsWords(days: readonly FindingsDay[]): string {
  const first = days[0];
  const last = days.at(-1);
  if (!first || !last) return "New findings per day: no days";
  const total = days.reduce((sum, d) => sum + dayTotal(d), 0);
  const head = `New findings per day from ${first.day} to ${last.day}: ${total.toLocaleString("en-US")} in total`;
  if (total === 0) return head;
  const busiest = days.reduce((top, d) => (dayTotal(d) > dayTotal(top) ? d : top), first);
  return `${head}, most on ${busiest.day} (${dayTotal(busiest).toLocaleString("en-US")})`;
}

/** When a source last fetched, as an ISO time, whichever shape the API sends. */
export function lastFetchAt(source: Pick<OverviewSource, "last_fetch">): string | null {
  const at = source.last_fetch;
  if (typeof at === "string") return at;
  return at?.finished_at ?? null;
}

// ---- geometry ----

/** The sparkline of a trend in a `width` × `height` box as SVG polyline points, oldest left.
 * The means span the box's height (a flat trend lies in the middle); one point becomes a flat
 * line over the whole width. */
export function sparklinePoints(trend: readonly TrendPoint[], width: number, height: number, pad = 2): string {
  const points = trend.filter((p) => Number.isFinite(p.mean));
  const means = points.map((p) => p.mean);
  if (means.length === 0) return "";
  const lo = Math.min(...means);
  const hi = Math.max(...means);
  const y = (m: number) => (hi === lo ? height / 2 : pad + ((hi - m) / (hi - lo)) * (height - 2 * pad));
  const round = (v: number) => Math.round(v * 100) / 100;
  if (means.length === 1) {
    const y0 = round(y(means[0] as number));
    return `0,${y0} ${width},${y0}`;
  }
  // By date, not by position: a gap of four weeks between two points takes four weeks' width.
  const at = points.map((p) => Date.parse(`${p.day}T00:00:00Z`));
  const first = at[0] as number;
  const span = (at.at(-1) as number) - first;
  const x = (i: number) => (span > 0 ? (((at[i] as number) - first) / span) * width : (i / (means.length - 1)) * width);
  return means.map((m, i) => `${round(x(i))},${round(y(m))}`).join(" ");
}

/** One segment of a stacked bar, from the baseline up: least severe at the bottom. */
export type BarSegment = { severity: Severity; y: number; height: number };

/** The segments of a day's bar in a chart `height` high whose tallest bar holds `max`. */
export function barSegments(day: SeverityCounts, max: number, height: number): BarSegment[] {
  if (max <= 0) return [];
  const out: BarSegment[] = [];
  let top = height;
  for (const severity of [...SEVERITIES].reverse()) {
    const n = day[severity] ?? 0;
    if (n <= 0) continue;
    const h = (n / max) * height;
    top -= h;
    out.push({ severity, y: top, height: h });
  }
  return out;
}

// ---- links into the inbox ----

/** The inbox search of the unresolved findings of one severity. The inbox shows open and
 * acknowledged findings by default, which is what the API counts. */
export function severityFindingsSearch(severity: Severity) {
  return { severity };
}
