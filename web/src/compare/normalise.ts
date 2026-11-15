// Normalisation of the compare view (spec 018, behaviour 3). Every mode is a positive affine
// map a·x + b computed from the raw-window `stats` of the chart response, never from the
// downsampled points, so the picture does not depend on the chart width. Pure.
import type { Norm } from "./url";

export type SeriesStats = { min: number; max: number; mean: number; sd: number; first: { ts: string; value: number } };

export type NormInput = { physical_min: number | null; physical_max: number | null; stats: SeriesStats | null };

export type NormMap = { a: number; b: number };

/** A series' map in a mode, or why the mode cannot show it. */
export type Normalised = { ok: true; map: NormMap } | { ok: false; reason: string };

export const IDENTITY: NormMap = { a: 1, b: 0 };
export const NO_STATS_REASON = "no usable samples in the window";
export const REASONS = {
  flatRange: "range is unavailable: the upper and lower limit are equal",
  flatZ: "z is unavailable: the values do not vary (sd = 0)",
  indexStart: "index is unavailable: the first value is zero or negative",
  indexSign: "index is unavailable: the series changes sign",
} as const;

const refuse = (reason: string): Normalised => ({ ok: false, reason });

/** The map of one series in `mode`. `raw` is the identity, whatever the stats. */
export function normalise(mode: Norm, series: NormInput): Normalised {
  if (mode === "raw") return { ok: true, map: IDENTITY };
  const stats = series.stats;
  if (!stats) return refuse(NO_STATS_REASON);
  if (mode === "range") {
    const limits = series.physical_min !== null && series.physical_max !== null;
    const lo = limits ? series.physical_min! : stats.min;
    const hi = limits ? series.physical_max! : stats.max;
    if (!(hi > lo)) return refuse(REASONS.flatRange);
    return { ok: true, map: { a: 1 / (hi - lo), b: -lo / (hi - lo) } };
  }
  if (mode === "z") {
    if (!(stats.sd > 0)) return refuse(REASONS.flatZ);
    return { ok: true, map: { a: 1 / stats.sd, b: -stats.mean / stats.sd } };
  }
  if (stats.min < 0 && stats.max > 0) return refuse(REASONS.indexSign);
  if (!(stats.first.value > 0)) return refuse(REASONS.indexStart);
  return { ok: true, map: { a: 100 / stats.first.value, b: 0 } };
}

/** The values under a map; nulls (line breaks) stay. */
export function applyNorm(values: readonly (number | null)[], map: NormMap): (number | null)[] {
  if (map === IDENTITY) return values.slice();
  return values.map((v) => (v === null ? null : map.a * v + map.b));
}
