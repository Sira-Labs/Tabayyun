// Fixtures and a fake backend of the overview's tests (spec 032).
import { METRICS, type FindingsDay, type Overview, type OverviewTile, type WorstItem } from "../overview";
import type { SourceSummary } from "../sources";
import { stubFetch } from "./helpers";
import { OPC_ID, PI_ID, summary } from "./sourcesFake";

export { OPC_ID, PI_ID };

export const SERIES_A = "cccccccc-0000-0000-0000-00000000c001";
export const SERIES_B = "cccccccc-0000-0000-0000-00000000c002";

/** A tile of `metric` whose trend falls from 97.1 to 96.4 over three scored days. */
export function tile(metric: OverviewTile["metric"], overrides: Partial<OverviewTile> = {}): OverviewTile {
  return {
    metric,
    mean: 96.4,
    min: 41,
    n_series: 9950,
    trend: [
      { day: "2026-10-23", mean: 97.1, n: 800 },
      { day: "2026-11-05", mean: 98.0, n: 9000 },
      { day: "2026-11-21", mean: 96.4, n: 9950 },
    ],
    ...overrides,
  };
}

/** A worst series with sensible defaults: Feeder 4 pressure, low on completeness. */
export function worstItem(overrides: Partial<WorstItem> = {}): WorstItem {
  return {
    series_id: SERIES_A,
    name: "Feeder 4 pressure",
    asset_path: "plant/feeder-4",
    overall: 88.2,
    metric: "completeness",
    value: 41,
    open_findings: 12,
    computed_at: "2026-11-21T06:00:00Z",
    ...overrides,
  };
}

/** The days of a window from `start`, with no findings unless `counts` gives them by index. */
export function findingDays(n: number, counts: Record<number, Partial<FindingsDay>> = {}): FindingsDay[] {
  const start = Date.UTC(2026, 10, 21) - (n - 1) * 86_400_000;
  return Array.from({ length: n }, (_, i) => ({
    day: new Date(start + i * 86_400_000).toISOString().slice(0, 10),
    critical: 0,
    high: 0,
    medium: 0,
    low: 0,
    ...counts[i],
  }));
}

/** The response of the spec's example, cut down: a workspace of 10 000 series. */
export function overview(overrides: Partial<Overview> = {}): Overview {
  return {
    window: { days: 30, start: "2026-10-23", end: "2026-11-21" },
    series: { total: 10000, scored: 9950 },
    tiles: METRICS.map((m) => tile(m, m === "overall" ? {} : { mean: m === "completeness" ? 65.5 : 92.25 })),
    worst: {
      share: 0.05,
      n: 498,
      items: [
        worstItem(),
        worstItem({ series_id: SERIES_B, name: "Feeder 5 flow", asset_path: null, overall: 90, metric: "timeliness", value: 55.5, open_findings: 0 }),
      ],
    },
    findings: {
      open: { critical: 1, high: 14, medium: 40, low: 3 },
      days: findingDays(30, { 28: { high: 2, medium: 5 }, 29: { critical: 1, high: 3, low: 1 } }),
    },
    sources: [
      { id: PI_ID, name: "Historian", type: "pi_web_api", enabled: true, status: "ok", last_fetch: "2026-11-21T05:00:00Z", n_series: 4200 },
      { id: OPC_ID, name: "Old OPC", type: "opc_ua", enabled: false, status: "failing", last_fetch: null, n_series: 12 },
    ],
    ...overrides,
  };
}

/** An empty workspace: nothing scored, no sources. */
export function emptyOverview(): Overview {
  return overview({
    series: { total: 0, scored: 0 },
    tiles: METRICS.map((m) => tile(m, { mean: null, min: null, n_series: 0, trend: [] })),
    worst: { share: 0.05, n: 0, items: [] },
    findings: { open: { critical: 0, high: 0, medium: 0, low: 0 }, days: findingDays(30) },
    sources: [],
  });
}

/** The sources the filter's select lists. */
export const SOURCES: SourceSummary[] = [
  summary({ id: PI_ID, name: "Historian" }),
  summary({ id: OPC_ID, name: "Old OPC", type: "opc_ua", enabled: false }),
];

export type OverviewBackend = {
  /** The answer to each overview request; the test may replace it. */
  answer: (query: URLSearchParams) => unknown;
  /** The overview requests so far, as their parameters. */
  asked: URLSearchParams[];
};

/** A fake of the overview's routes: `/api/overview` and the sources list. */
export function overviewBackend(answer: (query: URLSearchParams) => unknown = () => overview()): OverviewBackend {
  const state: OverviewBackend = { answer, asked: [] };
  stubFetch((url) => {
    const [path = "", query = ""] = url.split("?");
    if (path === "/api/overview") {
      const params = new URLSearchParams(query);
      state.asked.push(params);
      return state.answer(params);
    }
    if (path === "/api/sources") return { items: SOURCES };
    throw new Error(`unexpected ${url}`);
  });
  return state;
}
