import { describe, expect, it } from "vitest";
import { axisLanes } from "../compare/axes";
import { emphasisOf } from "../compare/findings";
import { normalise } from "../compare/normalise";
import { DASH, PALETTE, withAlpha } from "../compare/palette";
import { FINDING_FILL, buildLanes, residualLane, spreadLabels } from "../compare/plot";
import type { Norm } from "../compare/url";
import { H, IDS, T0, compareSeries, finding } from "./compareFake";

const RANGE = { from: T0, to: T0 + 72 * H };

function lanesOf(series: ReturnType<typeof compareSeries>[], norm: Norm = "raw", extra: Partial<Parameters<typeof buildLanes>[0]> = {}) {
  const norms = Object.fromEntries(series.map((s) => [s.id, normalise(norm, s)]));
  const drawn = series.filter((s) => s.ts.length > 0 && norms[s.id]!.ok);
  return buildLanes({ series, specs: axisLanes(drawn, "auto", norm), norms, theme: "light", emphasis: {}, findings: [], range: RANGE, ...extra });
}

describe("compare plot", () => {
  it("colours series in order and dashes the fifth to eighth", () => {
    const [lane] = lanesOf(Array.from({ length: 8 }, (_, i) => compareSeries(i)));
    expect(lane!.lines.map((l) => l.stroke)).toEqual(PALETTE.light);
    expect(lane!.lines.map((l) => l.dash)).toEqual([undefined, undefined, undefined, undefined, DASH, DASH, DASH, DASH]);
    expect(lane!.lines[0]!.ts).toEqual([T0 / 1e9, T0 / 1e9 + 3600]);
    expect(lane!.lines.every((l) => l.direct)).toBe(true);
  });

  it("draws the suspect thicker and mutes the others to 40 %", () => {
    const series = [0, 1, 2, 3].map((i) => compareSeries(i));
    const f = finding();
    const [lane] = lanesOf(series, "raw", { emphasis: emphasisOf(f, series.map((s) => s.id)), findings: [f], selected: f.id });
    expect(lane!.lines.map((l) => l.suspect)).toEqual([false, false, true, false]);
    expect(lane!.lines[2]!.width).toBeGreaterThan(lane!.lines[0]!.width);
    expect(lane!.lines[2]!.stroke).toBe(PALETTE.light[2]);
    expect(lane!.lines[0]!.stroke).toBe(withAlpha(PALETTE.light[0]!, 0.4));
  });

  it("shades a series' findings in its lane in its colour, cross-series findings in every lane", () => {
    const series = [compareSeries(0, { unit: "MW" }), compareSeries(1, { unit: "kV" }), compareSeries(2, { unit: "°C" })];
    const own = finding({ id: "own", series_id: IDS[1]!, evidence: {}, window: { start: T0 - H, end: T0 + 2 * H } });
    const cross = finding({ id: "cross", series_id: IDS[0]!, severity: "critical" });
    const outside = finding({ id: "outside", evidence: {}, window: { start: T0 + 100 * H, end: T0 + 101 * H } });
    const lanes = lanesOf(series, "raw", { findings: [own, cross, outside], selected: "cross" });
    expect(lanes).toHaveLength(3);
    const crossShade = { id: "cross", from: T0 + 30 * H, to: T0 + 36 * H, fill: FINDING_FILL.critical, selected: true };
    expect(lanes[0]!.shades).toEqual([crossShade]);
    expect(lanes[1]!.shades).toEqual([{ id: "own", from: T0, to: T0 + 2 * H, fill: withAlpha(PALETTE.light[1]!, 0.18), selected: false }, crossShade]);
    expect(lanes[2]!.shades).toEqual([crossShade]);
  });

  it("normalises lines from the stats and leaves out series without a line", () => {
    const series = [compareSeries(0), compareSeries(1, { stats: null }), compareSeries(2, { ts: [], values: [], stats: null })];
    const [lane, ...rest] = lanesOf(series, "index");
    expect(rest).toEqual([]);
    expect(lane!.lines.map((l) => l.id)).toEqual([IDS[0]]);
    expect(lane!.lines[0]!.values).toEqual([100, 200]);
    // Raw: the series without stats is drawn, the one without points is not.
    expect(lanesOf(series)[0]!.lines.map((l) => l.id)).toEqual([IDS[0], IDS[1]]);
  });

  it("builds the residual lane with a zero line and, for a balance, the loss band", () => {
    const base = { findings: [finding()], range: RANGE, theme: "light" as const };
    const ts = [String(T0), String(T0 + H)];
    const balance = residualLane({ kind: "balance", grid_ns: H, window: null, ts, residual: [1, 9], band: { lo: [0, 0], hi: [2, 2] }, missing: [] }, base)!;
    expect(balance.zeroLine).toBe(true);
    expect(balance.lines.map((l) => l.id)).toEqual(["residual", "band-hi", "band-lo"]);
    expect(balance.band).toEqual({ hi: 1, lo: 2 });
    expect(balance.shades).toHaveLength(1);
    const pair = residualLane({ kind: "pair", grid_ns: H, window: null, ts, residual: [1, null], band: null, missing: [] }, base)!;
    expect(pair.band).toBeNull();
    expect(pair.lines).toHaveLength(1);
    expect(residualLane({ kind: "balance", grid_ns: H, window: null, ts: [], residual: [], band: { lo: [], hi: [] }, missing: [IDS[1]!] }, base)).toBeNull();
  });

  it("moves labels apart without reordering them", () => {
    expect(spreadLabels([10, 12, 40, 11], 10)).toEqual([10, 30, 40, 20]);
  });
});
