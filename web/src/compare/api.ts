// The API of the compare view (spec 018): chart points for several series on the same bins,
// the residual of a balance or pair, and the group and finding the URL names.
import { apiGet } from "../api";
import { searchOfRange, type Range } from "../seriesChart";
import type { Finding } from "../types";
import type { SeriesStats } from "./normalise";

export type CompareSeries = {
  id: string;
  name: string;
  unit: string | null;
  physical_min: number | null;
  physical_max: number | null;
  /** ns strings; every finite raw sample's M4 points, whatever its quality. */
  ts: string[];
  /** null where the line breaks across a gap. */
  values: (number | null)[];
  n_raw: number;
  /** Of the raw usable samples in the window; null when there are none. */
  stats: SeriesStats | null;
};

/** `GET /api/compare/chart`; `bins` reports the window drawn (the default one when none was
 * asked). Its bounds are null only when no window was asked and no series has data. */
export type CompareChartResponse = {
  bins: { from: string | null; to: string | null; width_px: number };
  series: CompareSeries[];
};

/** `GET /api/series-groups/{id}/residual`; `ts` and `residual` are empty when `missing` is not.
 * A pair's residual is its first member minus its second. */
export type ResidualResponse = {
  kind: "balance" | "pair";
  grid_ns: number;
  window: { from: string; to: string } | null;
  ts: string[];
  residual: (number | null)[];
  band: { lo: (number | null)[]; hi: (number | null)[] } | null;
  missing: string[];
};

export type GroupMember = { series_id: string; external_id: string; role: string };

/** `GET /api/series-groups/{id}` (spec 008). */
export type SeriesGroup = {
  id: string;
  name: string;
  kind: string;
  members: GroupMember[];
  params: Record<string, unknown>;
};

function windowParams(range: Range | null, widthPx: number): URLSearchParams {
  const q = new URLSearchParams();
  if (range) {
    const { from, to } = searchOfRange(range);
    q.set("from", from);
    q.set("to", to);
  }
  q.set("width_px", String(widthPx));
  return q;
}

/** The chart request: one `series` parameter per id, then the window. */
export function compareChartUrl(ids: string[], range: Range | null, widthPx: number): string {
  const q = new URLSearchParams();
  for (const id of ids) q.append("series", id);
  for (const [k, v] of windowParams(range, widthPx)) q.set(k, v);
  return `/api/compare/chart?${q.toString()}`;
}

export const compareApi = {
  chart: (ids: string[], range: Range | null, widthPx: number) => apiGet<CompareChartResponse>(compareChartUrl(ids, range, widthPx)),
  residual: (groupId: string, range: Range, widthPx: number) =>
    apiGet<ResidualResponse>(`/api/series-groups/${encodeURIComponent(groupId)}/residual?${windowParams(range, widthPx).toString()}`),
  group: (id: string) => apiGet<SeriesGroup>(`/api/series-groups/${encodeURIComponent(id)}`),
  finding: (id: string) => apiGet<Finding>(`/api/findings/${encodeURIComponent(id)}`),
};

/** A balance or a two-member group has a residual lane (behaviour 5). */
export function hasResidual(group: Pick<SeriesGroup, "kind" | "members">): boolean {
  return group.kind === "balance" || group.members.length === 2;
}

const ROLE_ORDER: Record<string, number> = { input: 0, output: 1 };

/** Ids ordered by their role in the group: inputs, outputs, other members, then series outside it. */
export function orderByRole(ids: string[], group: Pick<SeriesGroup, "members"> | null | undefined): string[] {
  if (!group) return ids;
  const rank = (id: string) => {
    const member = group.members.find((m) => m.series_id === id);
    return member ? (ROLE_ORDER[member.role] ?? 2) : 3;
  };
  // Array.prototype.sort is stable: equal ranks keep the URL's order.
  return [...ids].sort((a, b) => rank(a) - rank(b));
}

/** The requested ids an error answer names (the API's 404 carries `detail.unknown`), in request order. */
export function unknownIdsOf(body: unknown, requested: string[]): string[] {
  const text = JSON.stringify(body ?? "").toLowerCase();
  return requested.filter((id) => text.includes(id.toLowerCase()));
}
