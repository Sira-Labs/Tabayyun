import { describe, expect, it } from "vitest";
import { CHART_BG, contrast } from "../compare/palette";
import { defaultPair, formatGrid, inFinding, involves, scaleOf, scatterPairOf, summaryText, ticksOf, timeColor, timePosition } from "../compare/scatter";
import { H, IDS, T0, finding } from "./compareFake";

const MIN = 60e9;

describe("compare scatter", () => {
  it("colour ramp: one ordered ramp per theme, readable at every step", () => {
    for (const dark of [false, true]) {
      const bg = CHART_BG[dark ? "dark" : "light"];
      const steps = [0, 0.25, 0.5, 0.75, 1].map((t) => timeColor(t, dark));
      for (const c of steps) expect(c).toMatch(/^#[0-9a-f]{6}$/);
      expect(steps[0]).not.toBe(steps[4]);
      // Lightness changes steadily in one direction, so the order also reads without hue,
      // and no step is lost on the background.
      const against = steps.map((c) => contrast(c, bg));
      const rising = against[4]! > against[0]!;
      for (let i = 1; i < against.length; i++) expect(against[i]! > against[i - 1]!).toBe(rising);
      for (const c of against) expect(c).toBeGreaterThanOrEqual(3);
      expect(contrast(steps[0]!, steps[4]!)).toBeGreaterThanOrEqual(3);
    }
    expect(timeColor(-1, false)).toBe(timeColor(0, false));
    expect(timeColor(2, true)).toBe(timeColor(1, true));
    expect(timeColor(Number.NaN, false)).toBe(timeColor(0, false));
  });

  it("colour ramp: a point's position in the window is clamped", () => {
    const window = { from: T0, to: T0 + 10 * H };
    expect(timePosition(T0, window)).toBe(0);
    expect(timePosition(T0 + 5 * H, window)).toBe(0.5);
    expect(timePosition(T0 + 10 * H, window)).toBe(1);
    expect(timePosition(T0 - H, window)).toBe(0);
    expect(timePosition(T0 + 11 * H, window)).toBe(1);
    expect(timePosition(T0, { from: T0, to: T0 })).toBe(0);
  });

  it("finding marks: a bin is marked when it overlaps a half-open window", () => {
    const windows = [{ window: { start: T0 + 2 * H, end: T0 + 4 * H } }];
    expect(inFinding(T0, H, windows)).toBe(false);
    // The bin ends where the window starts, or starts where it ends: outside.
    expect(inFinding(T0 + H, H, windows)).toBe(false);
    expect(inFinding(T0 + 4 * H, H, windows)).toBe(false);
    expect(inFinding(T0 + 1.5 * H, H, windows)).toBe(true);
    expect(inFinding(T0 + 2 * H, H, windows)).toBe(true);
    expect(inFinding(T0 + 3.5 * H, H, windows)).toBe(true);
    // A window shorter than a bin, inside it.
    expect(inFinding(T0, 6 * H, windows)).toBe(true);
    expect(inFinding(T0, H, [])).toBe(false);
    // A finding of one instant marks the bin that holds it.
    const instant = [{ window: { start: T0 + H, end: T0 + H } }];
    expect(inFinding(T0 + H, H, instant)).toBe(true);
    expect(inFinding(T0, H, instant)).toBe(false);
  });

  it("finding marks: the pair a finding names, and the default pair", () => {
    const shown = IDS.slice(0, 3);
    const pair = finding({ series_id: IDS[1]!, evidence: { partner: IDS[0] } });
    expect(scatterPairOf(pair, shown)).toEqual([IDS[1], IDS[0]]);
    expect(defaultPair(pair, shown)).toEqual([IDS[1], IDS[0]]);
    // The partner is not shown, the finding names several series, or it names none.
    expect(scatterPairOf(pair, [IDS[1]!, IDS[2]!])).toBeNull();
    expect(scatterPairOf(finding(), IDS.slice(0, 4))).toBeNull();
    expect(scatterPairOf(finding({ evidence: {} }), shown)).toBeNull();
    expect(scatterPairOf(undefined, shown)).toBeNull();
    expect(defaultPair(finding(), shown)).toEqual([IDS[0], IDS[1]]);
    expect(defaultPair(undefined, shown)).toEqual([IDS[0], IDS[1]]);
    expect(defaultPair(undefined, [IDS[0]!])).toBeUndefined();

    expect(involves(pair, [IDS[0]!, IDS[2]!])).toBe(true);
    expect(involves(pair, [IDS[1]!])).toBe(true);
    expect(involves(pair, [IDS[2]!])).toBe(false);
  });

  it("scales: the extent with room on each side and three to five round ticks", () => {
    const scale = scaleOf([10, 20, 30], 200);
    expect(scale.lo).toBe(9);
    expect(scale.hi).toBe(31);
    expect(scale.at(9)).toBe(0);
    expect(scale.at(31)).toBe(200);
    expect(scale.at(20)).toBeCloseTo(100);
    expect(scale.ticks).toEqual([10, 15, 20, 25, 30]);

    // A constant series still has an extent; no values at all do not break it.
    const flat = scaleOf([5, 5], 100);
    expect(flat.lo).toBeLessThan(5);
    expect(flat.hi).toBeGreaterThan(5);
    expect(flat.at(5)).toBeCloseTo(50);
    expect(scaleOf([0], 100).at(0)).toBeCloseTo(50);
    expect(Number.isFinite(scaleOf([], 100).at(0.5))).toBe(true);

    for (const [lo, hi] of [[0, 1], [0.1, 2.6], [-3.7, 12.2], [1000, 1001], [0.00012, 0.00091], [-5e6, 7e6], [99.5, 100.4]] as const) {
      const ticks = ticksOf(lo, hi);
      expect(ticks.length).toBeGreaterThanOrEqual(3);
      expect(ticks.length).toBeLessThanOrEqual(5);
      for (const t of ticks) expect(t >= lo - 1e-9 && t <= hi + 1e-9).toBe(true);
    }
    expect(ticksOf(0.1, 0.5)).toEqual([0.1, 0.2, 0.3, 0.4, 0.5]);
    expect(ticksOf(1, 1)).toEqual([]);

    // The ends of what a number can hold: no endless search for a step, no place that is not finite.
    expect(ticksOf(0, Number.MIN_VALUE)).toEqual([]);
    const tiny = scaleOf([0, Number.MIN_VALUE], 200);
    expect(Number.isFinite(tiny.at(0))).toBe(true);
    const huge = scaleOf([-1e308, 1e308], 200);
    expect([huge.lo, huge.hi].every(Number.isFinite)).toBe(true);
    expect(huge.at(-1e308)).toBeGreaterThanOrEqual(0);
    expect(huge.at(1e308)).toBeLessThanOrEqual(200);
    expect(huge.at(0)).toBeCloseTo(100);
  });

  it("summary text: bins, grid, points shown, r and the finding count", () => {
    const base = { n_bins: 672, shown: 400, gridNs: 15 * MIN, r: 0.934, inFindings: 37 };
    expect(summaryText(base)).toBe("672 bins on a 15 min grid, 400 shown · r = 0.93 · 37 inside finding windows");
    expect(summaryText({ ...base, shown: 672 })).toBe("672 bins on a 15 min grid · r = 0.93 · 37 inside finding windows");
    expect(summaryText({ n_bins: 1, shown: 1, gridNs: H, r: null, inFindings: 0 })).toBe("1 bin on a 1 h grid · r not defined · none inside finding windows");
    expect(summaryText({ n_bins: 12345, shown: 2000, gridNs: 30e9, r: -0.5, inFindings: 1 })).toBe("12,345 bins on a 30 s grid, 2,000 shown · r = -0.50 · 1 inside finding windows");
    expect(summaryText({ ...base, gridNs: 0 })).toBe("672 bins, 400 shown · r = 0.93 · 37 inside finding windows");
    expect(formatGrid(24 * H)).toBe("1 d");
    expect(formatGrid(90 * MIN)).toBe("90 min");
    expect(formatGrid(1.5e9)).toBe("1.5 s");
    expect(formatGrid(250e6)).toBe("0.25 s");
  });
});
