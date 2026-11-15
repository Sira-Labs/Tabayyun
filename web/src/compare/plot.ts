// What the compare chart draws (spec 018): per lane its axes, lines, shaded findings and, for
// the residual lane, the zero line and loss band. Pure; the uPlot wrapper only draws these.
import type { Range } from "../seriesChart";
import type { Finding, Severity } from "../types";
import type { CompareSeries, ResidualResponse } from "./api";
import type { AxisSpec, LaneSpec } from "./axes";
import { MUTED_OPACITY, isCrossSeries, type Emphasis } from "./findings";
import { applyNorm, type Normalised } from "./normalise";
import { seriesColour, seriesDash, withAlpha, type Theme } from "./palette";

const NS_PER_S = 1e9;
const LINE_WIDTH = 1.25;
const SUSPECT_WIDTH = 3;
const SERIES_SHADE_OPACITY = 0.18;

/** The colour of cross-series findings, by severity; translucent, so it reads on both themes. */
export const FINDING_FILL: Record<Severity, string> = {
  critical: "rgba(220, 38, 38, 0.22)",
  high: "rgba(234, 88, 12, 0.20)",
  medium: "rgba(217, 119, 6, 0.16)",
  low: "rgba(100, 116, 139, 0.16)",
};
const RESIDUAL_STROKE: Record<Theme, string> = { light: "#334155", dark: "#cbd5e1" };
const BAND_EDGE = "rgba(14, 165, 233, 0.55)";
export const BAND_FILL = "rgba(14, 165, 233, 0.12)";

export type PlotLine = {
  id: string;
  label: string;
  scale: string;
  stroke: string;
  width: number;
  dash?: number[];
  /** Named at the right end of the line. */
  direct: boolean;
  suspect: boolean;
  /** x in seconds, y null where the line breaks. */
  ts: number[];
  values: (number | null)[];
};

export type Shade = { id: string; from: number; to: number; fill: string; selected: boolean };

export type PlotLane = {
  key: string;
  height: number;
  axes: AxisSpec[];
  lines: PlotLine[];
  shades: Shade[];
  zeroLine: boolean;
  /** Indexes into `lines` of the band's upper and lower edge. */
  band: { hi: number; lo: number } | null;
};

export type LaneInput = {
  /** The series in display order; the colour follows the position. */
  series: CompareSeries[];
  specs: LaneSpec[];
  norms: Record<string, Normalised>;
  theme: Theme;
  emphasis: Record<string, Emphasis>;
  findings: Finding[];
  selected?: string;
  range: Range;
};

const seconds = (ts: string[]) => ts.map((t) => Number(t) / NS_PER_S);

/** Whether a series has a line: it has points, and the mode can show it. */
export function isDrawn(series: CompareSeries, norm: Normalised | undefined): boolean {
  return series.ts.length > 0 && norm?.ok === true;
}

function clip(finding: Finding, range: Range): { from: number; to: number } | null {
  if (finding.window.end <= range.from || finding.window.start >= range.to) return null;
  return { from: Math.max(finding.window.start, range.from), to: Math.min(finding.window.end, range.to) };
}

/** Cross-series findings in the finding colour, for every lane. */
function crossShades(input: Pick<LaneInput, "findings" | "selected" | "range">): Shade[] {
  return input.findings.filter(isCrossSeries).flatMap((f) => {
    const window = clip(f, input.range);
    return window ? [{ id: f.id, ...window, fill: FINDING_FILL[f.severity] ?? FINDING_FILL.low, selected: f.id === input.selected }] : [];
  });
}

/** The lanes of the series: lines normalised and styled, findings shaded. Series without a
 * line (no data, or refused by the mode) are left out; a lane left empty is dropped. */
export function buildLanes(input: LaneInput): PlotLane[] {
  const { series, specs, norms, theme, emphasis, findings, selected, range } = input;
  const index = new Map(series.map((s, i) => [s.id, i]));
  const cross = crossShades(input);
  const lanes = specs.map((spec): PlotLane => {
    const lines = spec.series.flatMap(({ id, scale }): PlotLine[] => {
      const i = index.get(id);
      const s = i === undefined ? undefined : series[i];
      const norm = norms[id];
      if (!s || i === undefined || !norm?.ok || !isDrawn(s, norm)) return [];
      const colour = seriesColour(i, theme);
      const mark = emphasis[id];
      return [
        {
          id,
          label: s.name,
          scale,
          stroke: mark === "muted" ? withAlpha(colour, MUTED_OPACITY) : colour,
          width: mark === "suspect" ? SUSPECT_WIDTH : LINE_WIDTH,
          dash: seriesDash(i),
          direct: true,
          suspect: mark === "suspect",
          ts: seconds(s.ts),
          values: applyNorm(s.values, norm.map),
        },
      ];
    });
    const own = findings
      .filter((f) => !isCrossSeries(f) && lines.some((l) => l.id === f.series_id))
      .flatMap((f): Shade[] => {
        const window = clip(f, range);
        const fill = withAlpha(seriesColour(index.get(f.series_id)!, theme), SERIES_SHADE_OPACITY);
        return window ? [{ id: f.id, ...window, fill, selected: f.id === selected }] : [];
      });
    const used = new Set(lines.map((l) => l.scale));
    return {
      key: spec.key,
      height: specs.length === 1 ? 320 : 170,
      axes: spec.axes.filter((a) => used.has(a.scale)),
      lines,
      shades: [...own, ...cross],
      zeroLine: false,
      band: null,
    };
  });
  return lanes.filter((l) => l.lines.length > 0);
}

/** The residual lane: the residual with a zero line, the loss band for a balance, and the
 * cross-series findings. Null when a member is missing or nothing can be drawn. */
export function residualLane(
  residual: ResidualResponse,
  input: Pick<LaneInput, "findings" | "selected" | "range" | "theme">,
): PlotLane | null {
  if (residual.missing.length > 0 || residual.ts.length === 0) return null;
  const ts = seconds(residual.ts);
  const base = { scale: "y0", direct: false, suspect: false, ts };
  const lines: PlotLine[] = [
    { ...base, id: "residual", label: residual.kind === "balance" ? "inputs − outputs" : "A − B", stroke: RESIDUAL_STROKE[input.theme], width: 1.5, direct: true, values: residual.residual },
  ];
  if (residual.band) {
    lines.push({ ...base, id: "band-hi", label: "loss band, upper", stroke: BAND_EDGE, width: 1, values: residual.band.hi });
    lines.push({ ...base, id: "band-lo", label: "loss band, lower", stroke: BAND_EDGE, width: 1, values: residual.band.lo });
  }
  return {
    key: "residual",
    height: 150,
    axes: [{ scale: "y0", label: "Residual", side: "left" }],
    lines,
    shades: crossShades(input),
    zeroLine: true,
    band: residual.band ? { hi: 1, lo: 2 } : null,
  };
}

/** y positions moved apart to at least `gap`, keeping their order, so labels do not overlap. */
export function spreadLabels(ys: number[], gap: number): number[] {
  const order = ys.map((y, i) => ({ y, i })).sort((a, b) => a.y - b.y);
  const out = ys.slice();
  let last = -Infinity;
  for (const { y, i } of order) {
    last = Math.max(y, last + gap);
    out[i] = last;
  }
  return out;
}
