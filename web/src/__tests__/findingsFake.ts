// Fixtures and a fake backend of the findings inbox's tests (spec 029).
import { vi } from "vitest";
import type { Assignee, BulkResult, FindingChange, FindingGroup, InboxFinding, SavedFilter } from "../findingsInbox";
import { json, stubFetch } from "./helpers";

export const T0 = 1_760_572_800_000_000_000;
const HOUR = 3_600e9;
export const SERIES_A = "5e7a1c2b-0000-4000-8000-00000000000a";
export const SERIES_B = "5e7a1c2b-0000-4000-8000-00000000000b";
export const PEOPLE: Assignee[] = [
  { id: "u1", display_name: "Ana" },
  { id: "u2", display_name: "Badr" },
];

/** The `n`th finding, an hour older than the one before. */
export function finding(n: number, overrides: Partial<InboxFinding> = {}): InboxFinding {
  return {
    id: `f-${n}`,
    check_id: "tby.spikes",
    series_id: SERIES_A,
    series_name: "Feeder 4 pressure",
    asset_path: "plant/feeder-4",
    dimension: "plausibility",
    severity: "high",
    window: { start: T0 - n * HOUR, end: T0 - n * HOUR + HOUR },
    score_impact: 1.5,
    summary: `Spike number ${n}`,
    evidence: {},
    status: "open",
    status_reason: null,
    status_at: null,
    muted_until: null,
    assignee: null,
    feedback: null,
    first_run_id: null,
    last_run_id: null,
    occurrences: 1,
    created_at: "2026-10-16T00:00:00Z",
    updated_at: "2026-10-16T00:00:00Z",
    ...overrides,
  };
}

export const GROUPS: FindingGroup[] = [
  { key: SERIES_A, label: "Feeder 4 pressure", count: 12, severities: { critical: 2, high: 10 }, latest: T0 },
  { key: SERIES_B, label: "Feeder 5 flow", count: 1, severities: { low: 1 }, latest: T0 - HOUR },
];

export type Backend = {
  /** What the list answers, by request; the default is `items` in one page. */
  items: InboxFinding[];
  list: (query: URLSearchParams) => { items: InboxFinding[]; next_cursor: string | null };
  groups: FindingGroup[];
  filters: SavedFilter[];
  /** Every GET, in order. */
  gets: string[];
  bulks: { ids: string[]; change: FindingChange }[];
  bulk: (ids: string[]) => BulkResult | Response;
  saves: { name: string; query: string }[];
  save: () => Response | null;
  deleted: string[];
  fetch: ReturnType<typeof stubFetch>;
};

/** A fake of the inbox's routes; the test changes its fields. */
export function backend(items: InboxFinding[], options: Parameters<typeof stubFetch>[1] = {}): Backend {
  const state: Backend = {
    items,
    list: () => ({ items: state.items, next_cursor: null }),
    groups: GROUPS,
    filters: [],
    gets: [],
    bulks: [],
    bulk: (ids) => ({ updated: ids, failed: [] }),
    saves: [],
    save: () => null,
    deleted: [],
    fetch: vi.fn() as unknown as ReturnType<typeof stubFetch>,
  };
  state.fetch = stubFetch((url, init) => {
    const [path = "", query = ""] = url.split("?");
    const method = init?.method ?? "GET";
    const body = typeof init?.body === "string" ? (JSON.parse(init.body) as unknown) : null;
    if (method === "GET") state.gets.push(url);
    if (path === "/api/findings/assignees") return { items: PEOPLE };
    if (path === "/api/findings/groups") return { items: state.groups, truncated: false };
    if (path === "/api/findings/export") return new Response("id\nf-1\n", { headers: { "Content-Type": "text/csv" } });
    if (path === "/api/findings/bulk" && method === "POST") {
      const sent = body as { ids: string[]; change: FindingChange };
      state.bulks.push(sent);
      return state.bulk(sent.ids);
    }
    if (path === "/api/findings") return state.list(new URLSearchParams(query));
    if (path === "/api/finding-filters" && method === "GET") return { items: state.filters };
    if (path === "/api/finding-filters" && method === "POST") {
      const sent = body as { name: string; query: string };
      state.saves.push(sent);
      const refused = state.save();
      if (refused) return refused;
      const saved = { id: `sf-${state.filters.length + 1}`, created_at: "2026-11-17T08:00:00Z", ...sent };
      state.filters = [...state.filters, saved];
      return json(saved, 201);
    }
    if (path.startsWith("/api/finding-filters/") && method === "DELETE") {
      const id = path.slice("/api/finding-filters/".length);
      state.deleted.push(id);
      state.filters = state.filters.filter((f) => f.id !== id);
      return new Response(null, { status: 204 });
    }
    throw new Error(`unexpected ${method} ${url}`);
  }, options);
  return state;
}

/** The list requests so far, as their parameters. */
export function listRequests(asked: Backend): URLSearchParams[] {
  return asked.gets.filter((url) => url.startsWith("/api/findings?")).map((url) => new URLSearchParams(url.split("?")[1]));
}

/** jsdom has no layout: give every element a size, so the virtualiser sees a viewport (it
 * reads `clientHeight` and `clientWidth` of the scroll element; older versions read the
 * offset sizes). Restored by the suite's `restoreAllMocks`. */
export function fakeLayout(height = 480, width = 1000) {
  for (const property of ["clientHeight", "offsetHeight"] as const) {
    vi.spyOn(HTMLElement.prototype, property, "get").mockReturnValue(height);
  }
  for (const property of ["clientWidth", "offsetWidth"] as const) {
    vi.spyOn(HTMLElement.prototype, property, "get").mockReturnValue(width);
  }
}
