// The scatter of the compare view (spec 033): the colour of a point's time, which points lie
// in finding windows, the pair a finding names, the plot's scales and the summary line. Pure.
import { formatNumber } from "../format";
import type { Range } from "../seriesChart";
import type { Finding } from "../types";
import { partnersOf } from "./findings";

type Rgb = [number, number, number];

// One ramp per theme from the window's start (blue) to its end (orange), the pair the
// research names for time in a scatter and one that does not lean on red against green. The
// end is the lighter of the two, and every step keeps 3:1 against the panel's background
// (WCAG 1.4.11), which `compare.scatter.test.ts` checks.
const RAMP: Record<"light" | "dark", [Rgb, Rgb]> = {
  light: [
    [0x0b, 0x3c, 0x7a],
    [0xd9, 0x73, 0x0d],
  ],
  dark: [
    [0x2f, 0x6f, 0xb5],
    [0xff, 0xd9, 0xa8],
  ],
};

const clamp01 = (t: number) => (Number.isFinite(t) ? Math.min(1, Math.max(0, t)) : 0);

/** The colour (`#rrggbb`) of a point at `t`, 0 for the window's start to 1 for its end. */
export function timeColor(t: number, dark: boolean): string {
  const [from, to] = RAMP[dark ? "dark" : "light"];
  const at = clamp01(t);
  return `#${from.map((c, i) => Math.round(c + (to[i]! - c) * at).toString(16).padStart(2, "0")).join("")}`;
}

/** Where `ts` lies in the window, from 0 to 1, clamped. */
export function timePosition(ts: number, window: Range): number {
  return window.to > window.from ? clamp01((ts - window.from) / (window.to - window.from)) : 0;
}

type Windowed = Pick<Finding, "window">;

/** Whether the bin `[ts, ts + gridNs)` overlaps a finding window. Windows are half-open like
 * the chart's shading, so a bin that only touches a window's end is outside; a window without
 * a length marks the bin that holds its instant. */
export function inFinding(ts: number, gridNs: number, findings: Windowed[]): boolean {
  const end = ts + Math.max(gridNs, 1);
  return findings.some(({ window }) => window.start < end && (window.end > window.start ? window.end > ts : window.start >= ts));
}

type Involved = Pick<Finding, "series_id" | "evidence">;

/** Whether a finding is about one of `seriesIds`: attached to it, or naming it as member or partner. */
export function involves(finding: Involved, seriesIds: string[]): boolean {
  return seriesIds.includes(finding.series_id) || partnersOf(finding).some((id) => seriesIds.includes(id));
}

/** The pair a finding names, its own series as x: only when it has exactly one partner and
 * both are among the series shown. */
export function scatterPairOf(finding: Involved | null | undefined, seriesIds: string[]): [string, string] | null {
  if (!finding) return null;
  const partners = partnersOf(finding);
  const partner = partners[0];
  if (partners.length !== 1 || partner === undefined) return null;
  return seriesIds.includes(finding.series_id) && seriesIds.includes(partner) ? [finding.series_id, partner] : null;
}

/** The pair "Scatter" opens with: the finding's, else the first two series; undefined with fewer than two. */
export function defaultPair(finding: Involved | null | undefined, seriesIds: string[]): [string, string] | undefined {
  const [first, second] = seriesIds;
  return scatterPairOf(finding, seriesIds) ?? (first !== undefined && second !== undefined ? [first, second] : undefined);
}

const STEPS = [1, 2, 2.5, 5, 10];
const MAX_TICKS = 5;

/** Three to five round tick values inside `[lo, hi]`. */
export function ticksOf(lo: number, hi: number): number[] {
  const span = hi - lo;
  if (!(span > 0) || !Number.isFinite(span)) return [];
  let magnitude = 10 ** (Math.floor(Math.log10(span)) - 1);
  for (;;) {
    for (const m of STEPS) {
      const step = m * magnitude;
      const first = Math.ceil(lo / step - 1e-9);
      const last = Math.floor(hi / step + 1e-9);
      if (last - first + 1 <= MAX_TICKS) {
        return Array.from({ length: last - first + 1 }, (_, i) => Number(((first + i) * step).toPrecision(12)));
      }
    }
    magnitude *= 10;
  }
}

export type Scale = { lo: number; hi: number; ticks: number[]; at: (value: number) => number };

// Room around the cloud, as a share of its extent.
const PAD = 0.05;

/** A linear scale of `values` onto `[0, pixels]`: their extent with a little room on each side
 * (a constant series gets room around its value), and its ticks. */
export function scaleOf(values: number[], pixels: number): Scale {
  const finite = values.filter(Number.isFinite);
  const min = finite.length > 0 ? Math.min(...finite) : 0;
  const max = finite.length > 0 ? Math.max(...finite) : 1;
  const pad = max > min ? (max - min) * PAD : Math.abs(min) * PAD || 1;
  const lo = min - pad;
  const hi = max + pad;
  return { lo, hi, ticks: ticksOf(lo, hi), at: (value) => ((value - lo) / (hi - lo)) * pixels };
}

const GRID_UNITS: [number, string][] = [
  [86_400e9, "d"],
  [3600e9, "h"],
  [60e9, "min"],
  [1e9, "s"],
];

/** A grid step in ns as `15 min`, `1 h`, `30 s` or `1 d`: the largest unit it is a whole
 * number of, else the largest unit below it. */
export function formatGrid(gridNs: number): string {
  const whole = GRID_UNITS.find(([ns]) => gridNs >= ns && gridNs % ns === 0);
  const [ns, unit] = whole ?? GRID_UNITS.find(([size]) => gridNs >= size) ?? GRID_UNITS.at(-1)!;
  return `${formatNumber(gridNs / ns)} ${unit}`;
}

export type Summary = { n_bins: number; shown: number; gridNs: number; r: number | null; inFindings: number };

/** `672 bins on a 15 min grid, 400 shown · r = 0.93 · 37 inside finding windows`. */
export function summaryText({ n_bins, shown, gridNs, r, inFindings }: Summary): string {
  const bins = `${formatNumber(n_bins)} ${n_bins === 1 ? "bin" : "bins"}${gridNs > 0 ? ` on a ${formatGrid(gridNs)} grid` : ""}`;
  return [
    shown === n_bins ? bins : `${bins}, ${formatNumber(shown)} shown`,
    r === null ? "r not defined" : `r = ${r.toFixed(2)}`,
    `${inFindings === 0 ? "none" : formatNumber(inFindings)} inside finding windows`,
  ].join(" · ");
}
