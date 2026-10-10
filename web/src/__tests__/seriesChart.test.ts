import { describe, expect, it } from "vitest";
import {
  MIN_SPAN_NS,
  aroundFinding,
  atLeastMin,
  findingRects,
  keyAction,
  pan,
  presetRange,
  rangeOfSearch,
  requestWidth,
  rugSegments,
  searchOfRange,
  toPlotData,
  validateSeriesPageSearch,
  zoom,
  type ChartResponse,
} from "../seriesChart";
import type { Finding } from "../types";

const H = 3600e9;
const T0 = 1_767_225_600_000_000_000;

function finding(id: string, start: number, end: number, severity: Finding["severity"] = "high"): Finding {
  return {
    id,
    check_id: "tby.spikes",
    series_id: "s1",
    dimension: "plausibility",
    severity,
    window: { start, end },
    score_impact: 0,
    summary: "",
    evidence: {},
    status: "open",
    status_reason: null,
    first_run_id: null,
    last_run_id: null,
    occurrences: 1,
    created_at: "",
    updated_at: "",
  };
}

describe("range arithmetic", () => {
  const r = { from: T0, to: T0 + 4 * H };

  it("zooms around the centre and pans by a fraction", () => {
    expect(zoom(r, 0.5)).toEqual({ from: T0 + H, to: T0 + 3 * H });
    expect(zoom(r, 2)).toEqual({ from: T0 - 2 * H, to: T0 + 6 * H });
    expect(pan(r, -0.5)).toEqual({ from: T0 - 2 * H, to: T0 + 2 * H });
  });

  it("never goes below one second", () => {
    const tiny = zoom({ from: T0, to: T0 + 1000 }, 0.5);
    expect(tiny.to - tiny.from).toBe(MIN_SPAN_NS);
    expect(atLeastMin(r)).toBe(r);
  });

  it("maps keys to actions and ignores others", () => {
    expect(keyAction("ArrowRight", r)).toEqual(pan(r, 0.5));
    expect(keyAction("ArrowLeft", r)).toEqual(pan(r, -0.5));
    expect(keyAction("+", r)).toEqual(zoom(r, 0.5));
    expect(keyAction("-", r)).toEqual(zoom(r, 2));
    expect(keyAction("Home", r)).toBe("all");
    expect(keyAction("a", r)).toBeNull();
  });

  it("presets end at the extent's end and stay inside it", () => {
    const extent = { from: T0, to: T0 + 48 * H };
    expect(presetRange(extent, H)).toEqual({ from: T0 + 47 * H, to: T0 + 48 * H });
    expect(presetRange(extent, 30 * 24 * H)).toEqual(extent);
  });

  it("pads a finding's window by a quarter each side", () => {
    expect(aroundFinding({ start: T0, end: T0 + 4 * H })).toEqual({ from: T0 - H, to: T0 + 5 * H });
  });

  it("requests widths in steps of 100 px between 100 and 4000", () => {
    expect([requestWidth(0), requestWidth(901), requestWidth(1200), requestWidth(9000)]).toEqual([100, 1000, 1200, 4000]);
  });
});

describe("URL search", () => {
  it("keeps a valid range and finding, drops the rest", () => {
    const finding = "0b8f9a2e-1c1d-4c1e-9a3b-0d6f4c2b1a00";
    expect(validateSeriesPageSearch({ from: "100", to: "200", finding, junk: 1 })).toEqual({ from: "100", to: "200", finding });
    expect(validateSeriesPageSearch({ from: "200", to: "100" })).toEqual({});
    expect(validateSeriesPageSearch({ from: "1e9", to: "2e9", finding: "x" })).toEqual({});
    expect(validateSeriesPageSearch({ from: "100" })).toEqual({});
  });

  it("round-trips a range", () => {
    const r = { from: T0, to: T0 + H };
    expect(rangeOfSearch(searchOfRange(r))).toEqual(r);
    expect(rangeOfSearch({})).toBeNull();
  });
});

describe("chart data", () => {
  const chart: ChartResponse = {
    window: { from: String(T0), to: String(T0 + 10 * H) },
    extent: { start: String(T0), end: String(T0 + 10 * H) },
    layer: "raw",
    width_px: 1200,
    n_raw: 3,
    ts: [String(T0), String(T0 + H), String(T0 + 2 * H)],
    values: [1, null, 3],
    quality: [
      { start: String(T0 + H), end: String(T0 + 2 * H), quality: "bad" },
      { start: String(T0 - 2 * H), end: String(T0 - H), quality: "uncertain" },
    ],
  };
  const range = { from: T0, to: T0 + 10 * H };

  it("gives uPlot seconds and keeps nulls", () => {
    expect(toPlotData(chart)).toEqual([[T0 / 1e9, T0 / 1e9 + 3600, T0 / 1e9 + 7200], [1, null, 3]]);
  });

  it("places rug segments in percent and drops those outside", () => {
    const [seg, ...rest] = rugSegments(chart.quality, range);
    expect(rest).toEqual([]);
    expect(seg).toMatchObject({ quality: "bad", left: 10, width: 10 });
    // A run of a microsecond still shows.
    const [thin] = rugSegments([{ start: String(T0), end: String(T0 + 1000), quality: "bad" }], range);
    expect(thin!.width).toBe(0.2);
  });

  it("clips finding rectangles to the range", () => {
    const rects = findingRects(
      [finding("a", T0 - H, T0 + H), finding("b", T0 + 20 * H, T0 + 21 * H), finding("c", T0 + 9 * H, T0 + 12 * H, "low")],
      range,
    );
    expect(rects).toEqual([
      { id: "a", from: T0, to: T0 + H, severity: "high" },
      { id: "c", from: T0 + 9 * H, to: T0 + 10 * H, severity: "low" },
    ]);
  });
});
