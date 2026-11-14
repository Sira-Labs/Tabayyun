import { describe, expect, it } from "vitest";
import { NO_STATS_REASON, REASONS, applyNorm, normalise, type NormInput, type SeriesStats } from "../compare/normalise";

const stats = (over: Partial<SeriesStats> = {}): SeriesStats => ({ min: 10, max: 30, mean: 20, sd: 5, first: { ts: "0", value: 25 }, ...over });
const input = (over: Partial<NormInput> = {}): NormInput => ({ physical_min: null, physical_max: null, stats: stats(), ...over });

/** The normalised values of a series, or the refusal. */
function run(mode: Parameters<typeof normalise>[0], series: NormInput, values: (number | null)[]) {
  const n = normalise(mode, series);
  return n.ok ? applyNorm(values, n.map) : n.reason;
}

describe("normalise_modes", () => {
  it("range uses the physical limits when both are known", () => {
    expect(run("range", input({ physical_min: 0, physical_max: 100 }), [0, 25, 100])).toEqual([0, 0.25, 1]);
  });

  it("range falls back to the window's minimum and maximum", () => {
    expect(run("range", input(), [10, 20, 30])).toEqual([0, 0.5, 1]);
    // One limit alone is not a range.
    expect(run("range", input({ physical_min: 0 }), [10, 30])).toEqual([0, 1]);
  });

  it("range is refused when hi = lo", () => {
    expect(run("range", input({ stats: stats({ min: 7, max: 7 }) }), [7])).toBe(REASONS.flatRange);
    expect(run("range", input({ physical_min: 5, physical_max: 5 }), [7])).toBe(REASONS.flatRange);
  });

  it("z subtracts the mean and divides by the standard deviation", () => {
    expect(run("z", input(), [20, 25, 10, null])).toEqual([0, 1, -2, null]);
  });

  it("z is refused when sd = 0", () => {
    expect(run("z", input({ stats: stats({ sd: 0 }) }), [20])).toBe(REASONS.flatZ);
  });

  it("index is 100 at the first usable raw sample", () => {
    expect(run("index", input(), [25, 50, 12.5])).toEqual([100, 200, 50]);
  });

  it("index is refused when x₀ ≤ 0", () => {
    expect(run("index", input({ stats: stats({ min: 0, first: { ts: "0", value: 0 } }) }), [0, 5])).toBe(REASONS.indexStart);
    expect(run("index", input({ stats: stats({ min: -30, max: -10, first: { ts: "0", value: -20 } }) }), [-20])).toBe(REASONS.indexStart);
  });

  it("index is refused when the series changes sign", () => {
    expect(run("index", input({ stats: stats({ min: -5, max: 30 }) }), [25])).toBe(REASONS.indexSign);
  });

  it("gives identical output at two chart widths, since it reads the window's stats", () => {
    // The same raw window downsampled at two widths: different points, the same stats.
    const series = input({ stats: stats({ min: 12, max: 28, mean: 19, sd: 4, first: { ts: "0", value: 16 } }) });
    const wide = [16, 12, 28, 20, 24, 14];
    const narrow = [16, 12, 28];
    for (const mode of ["range", "z", "index"] as const) {
      const a = run(mode, series, wide) as number[];
      const b = run(mode, series, narrow) as number[];
      expect(b).toEqual(a.slice(0, 3));
      // And nothing comes from the points: stats of the narrow points alone would differ.
      expect(normalise(mode, series)).toEqual(normalise(mode, { ...series }));
    }
    expect(run("z", series, narrow)).toEqual([-0.75, -1.75, 2.25]);
  });

  it("hides a series with null stats, with its reason, in every normalised mode", () => {
    const series = input({ stats: null, physical_min: 0, physical_max: 100 });
    for (const mode of ["range", "z", "index"] as const) {
      expect(normalise(mode, series)).toEqual({ ok: false, reason: NO_STATS_REASON });
    }
    expect(NO_STATS_REASON).toBe("no usable samples in the window");
    expect(run("raw", series, [1, null, 3])).toEqual([1, null, 3]);
  });

  it("every map is positive affine, so order and gaps are kept", () => {
    for (const mode of ["range", "z", "index"] as const) {
      const n = normalise(mode, input());
      expect(n.ok && n.map.a > 0).toBe(true);
    }
  });
});
