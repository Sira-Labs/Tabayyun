// Chart data and range arithmetic of the series page (spec 025). Pure functions, so the page
// and its tests share them; the uPlot wrapper only draws what these return.
import { allPages, apiGet } from "./api";
import type { Finding, Severity } from "./types";

/** A half-open time range in ns since the epoch. JS numbers keep ns to about 256 ns, plenty for
 * a chart; the URL carries them as integer strings. */
export type Range = { from: number; to: number };

export type QualityClass = "uncertain" | "bad" | "estimated";

/** `GET /api/series/{id}/chart`; times are ns strings, `values` null where the line breaks. */
export type ChartResponse = {
  window: { from: string; to: string } | null;
  extent: { start: string; end: string } | null;
  layer: string;
  width_px: number;
  n_raw: number;
  ts: string[];
  values: (number | null)[];
  quality: { start: string; end: string; quality: QualityClass }[];
};

export type Band = { lo: number; hi: number; source: "metadata" | "profile" };

/** The core profile fields the page shows (the server sends more). */
export type Profile = {
  n_samples: number;
  n_finite: number;
  expected_interval_ns: number | null;
  median: number | null;
  mad: number | null;
  p001: number | null;
  p999: number | null;
  min: number | null;
  max: number | null;
  resolution: number | null;
  noise_mad: number | null;
  dominant_period_ns: number | null;
  seasonal_strength: number | null;
};

/** `GET /api/series/{id}/profile`. */
export type ProfileResponse = {
  window: { from: string; to: string } | null;
  profile: Profile | null;
  quality_counts: Record<"good" | "uncertain" | "bad" | "estimated", number>;
  band: Band | null;
};

const NS_PER_S = 1e9;
export const MIN_SPAN_NS = NS_PER_S;
const HOUR_NS = 3600 * NS_PER_S;
const DAY_NS = 24 * HOUR_NS;

export const PRESETS = [
  { key: "1h", label: "1 h", span: HOUR_NS },
  { key: "1d", label: "1 d", span: DAY_NS },
  { key: "7d", label: "7 d", span: 7 * DAY_NS },
  { key: "30d", label: "30 d", span: 30 * DAY_NS },
] as const;

/** Chart widths are requested in steps of 100 px, so small resizes reuse the cached answer. */
export function requestWidth(cssWidth: number): number {
  const stepped = Math.ceil(Math.max(cssWidth, 1) / 100) * 100;
  return Math.min(4000, Math.max(100, stepped));
}

/** A range at least `MIN_SPAN_NS` wide, widened around its centre when narrower. */
export function atLeastMin(range: Range): Range {
  if (range.to - range.from >= MIN_SPAN_NS) return range;
  const mid = (range.from + range.to) / 2;
  return { from: Math.round(mid - MIN_SPAN_NS / 2), to: Math.round(mid + MIN_SPAN_NS / 2) };
}

/** The range zoomed by `factor` around its centre (< 1 zooms in). */
export function zoom(range: Range, factor: number): Range {
  const mid = (range.from + range.to) / 2;
  const half = ((range.to - range.from) * factor) / 2;
  return atLeastMin({ from: Math.round(mid - half), to: Math.round(mid + half) });
}

/** The range moved by `fraction` of its width (negative moves back in time). */
export function pan(range: Range, fraction: number): Range {
  const shift = Math.round((range.to - range.from) * fraction);
  return { from: range.from + shift, to: range.to + shift };
}

/** The last `span` ns of the extent, or the whole extent when it is shorter. */
export function presetRange(extent: Range, span: number): Range {
  return { from: Math.max(extent.from, extent.to - span), to: extent.to };
}

/** What a key on the focused chart does: a new range, "all", or null when the key is not ours. */
export function keyAction(key: string, range: Range): Range | "all" | null {
  switch (key) {
    case "ArrowLeft":
      return pan(range, -0.5);
    case "ArrowRight":
      return pan(range, 0.5);
    case "+":
    case "=":
      return zoom(range, 0.5);
    case "-":
    case "_":
      return zoom(range, 2);
    case "Home":
      return "all";
    default:
      return null;
  }
}

/** A finding's window plus a quarter of its width on each side. */
export function aroundFinding(window: { start: number; end: number }): Range {
  const pad = (window.end - window.start) / 4;
  return atLeastMin({ from: Math.round(window.start - pad), to: Math.round(window.end + pad) });
}

/** uPlot's aligned data: x in seconds, y with null where the line breaks. */
export function toPlotData(chart: ChartResponse): [number[], (number | null)[]] {
  return [chart.ts.map((t) => Number(t) / NS_PER_S), chart.values];
}

export type Rect = { id: string; from: number; to: number; severity: Severity };

/** Finding windows clipped to the range, as shaded rectangles; ones outside it are left out. */
export function findingRects(findings: Finding[], range: Range): Rect[] {
  return findings
    .filter((f) => f.window.end > range.from && f.window.start < range.to)
    .map((f) => ({
      id: f.id,
      from: Math.max(f.window.start, range.from),
      to: Math.min(f.window.end, range.to),
      severity: f.severity,
    }));
}

export type Segment = { left: number; width: number; quality: QualityClass; from: number; to: number };

/** Quality runs as rug segments: left and width in % of the range, at least 0.2 % wide. */
export function rugSegments(quality: ChartResponse["quality"], range: Range): Segment[] {
  const span = range.to - range.from;
  if (span <= 0) return [];
  return quality
    .map((q) => ({ from: Math.max(Number(q.start), range.from), to: Math.min(Number(q.end), range.to), quality: q.quality }))
    .filter((q) => q.to > q.from)
    .map((q) => ({
      ...q,
      left: ((q.from - range.from) / span) * 100,
      width: Math.max(0.2, ((q.to - q.from) / span) * 100),
    }));
}

/** A range in the URL search: both bounds as integer ns strings, `to` after `from`. */
export function rangeOfSearch(search: { from?: string; to?: string }): Range | null {
  if (!search.from || !search.to) return null;
  const from = Number(search.from);
  const to = Number(search.to);
  return Number.isFinite(from) && Number.isFinite(to) && to > from ? { from, to } : null;
}

/** Validated URL search of `/series/$seriesId`: unknown keys and malformed values are dropped. */
export type SeriesPageSearch = { from?: string; to?: string; finding?: string };

const NS_RE = /^-?\d{1,19}$/;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function validateSeriesPageSearch(search: Record<string, unknown>): SeriesPageSearch {
  const out: SeriesPageSearch = {};
  const ns = (v: unknown) => (typeof v === "string" || typeof v === "number") && NS_RE.test(String(v)) ? String(v) : undefined;
  const from = ns(search.from);
  const to = ns(search.to);
  if (from && to && rangeOfSearch({ from, to })) {
    out.from = from;
    out.to = to;
  }
  if (typeof search.finding === "string" && UUID_RE.test(search.finding)) out.finding = search.finding;
  return out;
}

/** A range as URL search values. */
export function searchOfRange(range: Range): { from: string; to: string } {
  return { from: String(Math.round(range.from)), to: String(Math.round(range.to)) };
}

function q(params: Record<string, string | number | undefined>): string {
  const s = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined) s.set(k, String(v));
  return s.toString();
}

export const chartApi = {
  chart: (seriesId: string, range: Range | null, widthPx: number) =>
    apiGet<ChartResponse>(
      `/api/series/${encodeURIComponent(seriesId)}/chart?${q({ ...(range ? searchOfRange(range) : {}), width_px: widthPx })}`,
    ),
  profile: (seriesId: string) => apiGet<ProfileResponse>(`/api/series/${encodeURIComponent(seriesId)}/profile`),
  findings: (seriesId: string, range: Range) =>
    allPages<Finding>(
      (cursor) =>
        `/api/findings?${q({ series_id: seriesId, since: searchOfRange(range).from, until: searchOfRange(range).to, status: "all", limit: 500, cursor })}`,
    ),
};

