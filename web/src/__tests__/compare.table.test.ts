import { describe, expect, it } from "vitest";
import { binIndex, binWindow, binsOf, findingsCsv, readoutAt, stepBin, tableCsv, tableRows } from "../compare/table";
import { H, T0, compareSeries, finding } from "./compareFake";

// Four one-hour bins.
const BINS = { from: T0, to: T0 + 4 * H, width: 4 };
const at = (h: number) => String(T0 + h * H);
const A = compareSeries(0, { name: "F1", unit: "MW", ts: [at(0), at(0.2), at(0.5), at(2.1)], values: [5, 9, 1, 4] });
const B = compareSeries(1, { name: 'F2, "east"', unit: null, ts: [at(0.4), at(3.9)], values: [7, null] });

describe("compare table", () => {
  it("reads the bins of a chart answer", () => {
    expect(binsOf({ bins: { from: String(T0), to: String(T0 + 4 * H), width_px: 4 }, series: [] })).toEqual(BINS);
    expect(binsOf({ bins: { from: null, to: null, width_px: 1200 }, series: [] })).toBeNull();
    expect([binIndex(T0, BINS), binIndex(T0 + 1.5 * H, BINS), binIndex(T0 + 4 * H, BINS), binIndex(T0 - H, BINS)]).toEqual([0, 1, 3, 0]);
    expect(binWindow(2, BINS)).toEqual({ from: T0 + 2 * H, to: T0 + 3 * H });
  });

  it("table_view_rows: one row per bin with each series' min and max", () => {
    expect(tableRows([A, B], BINS)).toEqual([
      { bin: 0, from: T0, to: T0 + H, cells: [{ min: 1, max: 9 }, { min: 7, max: 7 }] },
      { bin: 2, from: T0 + 2 * H, to: T0 + 3 * H, cells: [{ min: 4, max: 4 }, null] },
    ]);
  });

  it("table_view_rows: exports the rows as CSV", () => {
    expect(tableCsv([A, B], tableRows([A, B], BINS))).toBe(
      [
        'bin_start_utc,bin_end_utc,F1 min (MW),F1 max (MW),"F2, ""east"" min","F2, ""east"" max"',
        "2026-01-01T00:00:00.000Z,2026-01-01T01:00:00.000Z,1,9,7,7",
        "2026-01-01T02:00:00.000Z,2026-01-01T03:00:00.000Z,4,4,,",
        "",
      ].join("\r\n"),
    );
    const csv = findingsCsv([finding({ summary: "=cmd()" })], () => "F3");
    expect(csv.split("\r\n")[1]).toBe("2026-01-02T06:00:00.000Z,2026-01-02T12:00:00.000Z,F3,tby.balance_residual,high,open,'=cmd()");
  });

  it("reads, per series, the point nearest the cursor inside the bin", () => {
    expect(readoutAt([A, B], BINS, 0, T0 + 0.25 * H)).toEqual([
      { id: A.id, ts: T0 + 0.2 * H, value: 9 },
      { id: B.id, ts: T0 + 0.4 * H, value: 7 },
    ]);
    expect(readoutAt([A, B], BINS, 2, T0 + 2.5 * H)).toEqual([{ id: A.id, ts: T0 + 2.1 * H, value: 4 }, null]);
  });

  it("steps the cursor through the bins that hold data", () => {
    const rows = tableRows([A, B], BINS);
    expect(stepBin(rows, null, 1)).toBe(0);
    expect(stepBin(rows, null, -1)).toBe(2);
    expect(stepBin(rows, 0, 1)).toBe(2);
    expect(stepBin(rows, 2, 1)).toBe(2);
    expect(stepBin(rows, 2, -1)).toBe(0);
    // From an empty bin between two rows.
    expect(stepBin(rows, 1, 1)).toBe(2);
    expect(stepBin(rows, 1, -1)).toBe(0);
    expect(stepBin(rows, 3, -1)).toBe(2);
    expect(stepBin([], 0, 1)).toBeNull();
  });
});
