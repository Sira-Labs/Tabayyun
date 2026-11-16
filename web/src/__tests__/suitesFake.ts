// Fixtures of the suites pages' tests (spec 027).
import type { CatalogueCheck, Dataset, Suite, SuiteCheck } from "../suites";

export const SUITE_ID = "cccccccc-0000-0000-0000-000000000001";
export const DATASET_ID = "dddddddd-0000-0000-0000-000000000001";
export const RUN_ID = "eeeeeeee-0000-0000-0000-000000000001";

/** A small catalogue with every kind of parameter: number, switch, text, list, decided at run time. */
export const CATALOGUE: CatalogueCheck[] = [
  { id: "spike", scope: "series", defaults: { threshold: 4, window: 25, two_sided: true }, auto: {} },
  {
    id: "range",
    scope: "series",
    defaults: { min: null, max: null, mode: "physical", ignore: [] },
    auto: {
      min: { source: "metadata", note: "the series' physical minimum" },
      max: { source: "metadata", note: "the series' physical maximum" },
    },
  },
  {
    id: "drift",
    scope: "group",
    defaults: { tolerance: null },
    auto: { tolerance: { source: "baseline", note: "three times the baseline's spread" } },
  },
];

export const DATASETS: { items: Dataset[]; next_cursor: null } = {
  items: [
    { id: DATASET_ID, name: "Feeders", series: [{ id: "s1", external_id: "feeder-1" }], window: { last: "7d" }, created_at: "2026-10-01T00:00:00Z" },
    { id: "dddddddd-0000-0000-0000-000000000002", name: "Pumps", series: [], window: { last: "1d" }, created_at: "2026-10-02T00:00:00Z" },
  ],
  next_cursor: null,
};

const CHECKS: SuiteCheck[] = [
  {
    id: "spike",
    scope: "series",
    enabled: true,
    params: { threshold: 6 },
    thresholds: [
      { param: "threshold", value: 6, source: "override", note: null },
      { param: "window", value: 25, source: "default", note: null },
      { param: "two_sided", value: true, source: "default", note: null },
    ],
  },
  {
    id: "range",
    scope: "series",
    enabled: true,
    params: {},
    thresholds: [
      { param: "min", value: null, source: "metadata", note: "the series' physical minimum" },
      { param: "max", value: null, source: "metadata", note: "the series' physical maximum" },
      { param: "mode", value: "physical", source: "default", note: null },
      { param: "ignore", value: [], source: "default", note: null },
    ],
  },
  {
    id: "drift",
    scope: "group",
    enabled: false,
    params: {},
    thresholds: [{ param: "tolerance", value: null, source: "baseline", note: "three times the baseline's spread" }],
  },
];

export function suite(overrides: Partial<Suite> = {}): Suite {
  return {
    id: SUITE_ID,
    name: "Feeders, hourly",
    dataset_id: DATASET_ID,
    dataset_name: "Feeders",
    schedule: "5 * * * *",
    enabled: true,
    checks: CHECKS,
    next_run_at: "2026-10-09T09:05:00Z",
    last_run_at: "2026-10-09T08:05:00Z",
    last_run: { id: RUN_ID, status: "succeeded", created_at: "2026-10-09T08:05:00Z" },
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    ...overrides,
  };
}

/** The body of a request, parsed. */
export const bodyOf = (init?: RequestInit): unknown => (init?.body ? JSON.parse(String(init.body)) : null);
