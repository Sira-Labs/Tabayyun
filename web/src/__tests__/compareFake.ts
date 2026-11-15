// Fixtures of the compare view tests (spec 018).
import type { CompareSeries } from "../compare/api";
import type { Finding } from "../types";

export const H = 3600e9;
export const T0 = 1_767_225_600_000_000_000;

export const IDS = Array.from({ length: 9 }, (_, i) => `5e7a1c2b-0000-4000-8000-00000000000${i + 1}`);
export const GROUP_ID = "9a000000-0000-4000-8000-000000000001";
export const FINDING_ID = "f0000000-0000-4000-8000-000000000001";

/** A series with two points an hour apart and stats matching them. */
export function compareSeries(n: number, overrides: Partial<CompareSeries> = {}): CompareSeries {
  return {
    id: IDS[n]!,
    name: `F${n + 1}`,
    unit: "MW",
    physical_min: null,
    physical_max: null,
    ts: [String(T0), String(T0 + H)],
    values: [10, 20],
    n_raw: 2,
    stats: { min: 10, max: 20, mean: 15, sd: 5, first: { ts: String(T0), value: 10 } },
    ...overrides,
  };
}

export function finding(overrides: Partial<Finding> = {}): Finding {
  return {
    id: FINDING_ID,
    check_id: "tby.balance_residual",
    series_id: IDS[2]!,
    dimension: "consistency",
    severity: "high",
    window: { start: T0 + 30 * H, end: T0 + 36 * H },
    score_impact: 1,
    summary: "Balance SS-North does not close for 6h; F3 explains most of it",
    evidence: { group_id: GROUP_ID, group_name: "SS-North", members: IDS.slice(0, 4), suspect: IDS[2] },
    status: "open",
    status_reason: null,
    first_run_id: null,
    last_run_id: null,
    occurrences: 1,
    created_at: "",
    updated_at: "",
    ...overrides,
  };
}
