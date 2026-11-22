// URL state of `/compare` (specs 018 and 033): the whole view lives in the query string, so a link
// restores it. Pure functions, shared by the router, the page and the entry-point links.
import { rangeOfSearch, searchOfRange, type Range } from "../seriesChart";

export const LAYOUTS = ["auto", "overlay", "lanes"] as const;
export const NORMS = ["raw", "range", "z", "index"] as const;
export type Layout = (typeof LAYOUTS)[number];
export type Norm = (typeof NORMS)[number];

export const MIN_SERIES = 2;
export const MAX_SERIES = 8;
// More ids than this in a URL are cut; the page still says there are too many.
const MAX_IDS_KEPT = 32;

/** The route's search: every value a string, defaults left out. */
export type CompareSearch = {
  series?: string;
  from?: string;
  to?: string;
  layout?: Layout;
  norm?: Norm;
  group?: string;
  finding?: string;
  xy?: string;
};

/** The view the search describes. `series` keeps the URL's order; `range` null is the API's default window. */
export type CompareState = {
  series: string[];
  range: Range | null;
  layout: Layout;
  norm: Norm;
  group?: string;
  finding?: string;
  /** The scatter's x and y series (spec 033); both are among `series`. */
  xy?: [string, string];
};

const NS_RE = /^-?\d{1,19}$/;
export const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const ID_TOKEN_RE = /^[\w.:-]{1,64}$/;

/** The ids of a comma-separated list: trimmed, well-formed tokens only, duplicates dropped. */
export function splitIds(value: string): string[] {
  const ids: string[] = [];
  for (const raw of value.split(",")) {
    const id = raw.trim();
    if (ID_TOKEN_RE.test(id) && !ids.includes(id)) ids.push(id);
    if (ids.length === MAX_IDS_KEPT) break;
  }
  return ids;
}

/** Validated search of `/compare`: unknown keys, malformed values and defaults are dropped. */
export function validateCompareSearch(search: Record<string, unknown>): CompareSearch {
  const out: CompareSearch = {};
  if (typeof search.series === "string") {
    const ids = splitIds(search.series);
    if (ids.length) out.series = ids.join(",");
  }
  const ns = (v: unknown) => ((typeof v === "string" || typeof v === "number") && NS_RE.test(String(v)) ? String(v) : undefined);
  const from = ns(search.from);
  const to = ns(search.to);
  if (from && to && rangeOfSearch({ from, to })) {
    out.from = from;
    out.to = to;
  }
  if (LAYOUTS.includes(search.layout as Layout) && search.layout !== "auto") out.layout = search.layout as Layout;
  if (NORMS.includes(search.norm as Norm) && search.norm !== "raw") out.norm = search.norm as Norm;
  if (typeof search.group === "string" && UUID_RE.test(search.group)) out.group = search.group;
  if (typeof search.finding === "string" && UUID_RE.test(search.finding)) out.finding = search.finding;
  if (typeof search.xy === "string") {
    const pair = search.xy.split(",").map((id) => id.trim());
    const shown = out.series ? out.series.split(",") : [];
    if (pair.length === 2 && pair[0] !== pair[1] && pair.every((id) => UUID_RE.test(id) && shown.includes(id))) out.xy = pair.join(",");
  }
  return out;
}

/** The view of a validated search. */
export function stateOfSearch(search: CompareSearch): CompareState {
  return {
    series: search.series ? splitIds(search.series) : [],
    range: rangeOfSearch(search),
    layout: search.layout ?? "auto",
    norm: search.norm ?? "raw",
    ...(search.group ? { group: search.group } : {}),
    ...(search.finding ? { finding: search.finding } : {}),
    ...(search.xy ? { xy: search.xy.split(",") as [string, string] } : {}),
  };
}

/** The search of a view, in the documented key order. */
export function searchOfState(state: CompareState): CompareSearch {
  return validateCompareSearch({
    series: state.series.join(","),
    ...(state.range ? searchOfRange(state.range) : {}),
    layout: state.layout,
    norm: state.norm,
    group: state.group,
    finding: state.finding,
    xy: state.xy?.join(","),
  });
}

/** A view as a plain query string (no leading `?`), ids separated by literal commas. */
export function queryOfState(state: CompareState): string {
  return Object.entries(searchOfState(state))
    .map(([key, value]) => `${key}=${key === "series" || key === "xy" ? value.split(",").map(encodeURIComponent).join(",") : encodeURIComponent(value)}`)
    .join("&");
}

/** The view of a plain query string. */
export function stateOfQuery(query: string): CompareState {
  const params = new URLSearchParams(query.startsWith("?") ? query.slice(1) : query);
  return stateOfSearch(validateCompareSearch(Object.fromEntries(params.entries())));
}
