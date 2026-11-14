// Which series share a y-axis and a lane (spec 018, behaviour 2). Pure.
import type { Layout, Norm } from "./url";

export type AxisSeries = { id: string; unit: string | null };

/** Series that share a unit; series without one form a group of their own. */
export type UnitGroup = { unit: string | null; label: string; ids: string[] };

export type AxisSide = "left" | "right" | "none";
export type AxisSpec = { scale: string; label: string; side: AxisSide };

/** One plot: its y-axes and which scale each of its series is drawn on. */
export type LaneSpec = { key: string; axes: AxisSpec[]; series: { id: string; scale: string }[] };

export const NO_UNIT_LABEL = "no unit";

export const NORM_AXIS_LABEL: Record<Exclude<Norm, "raw">, string> = {
  range: "Share of range (0 to 1)",
  z: "z-score in the visible window",
  index: "Index (first value = 100)",
};

/** Unit groups in order of first appearance. */
export function unitGroups(series: AxisSeries[]): UnitGroup[] {
  const groups: UnitGroup[] = [];
  for (const s of series) {
    const unit = s.unit?.trim() ? s.unit.trim() : null;
    const group = groups.find((g) => g.unit === unit);
    if (group) group.ids.push(s.id);
    else groups.push({ unit, label: unit ?? NO_UNIT_LABEL, ids: [s.id] });
  }
  return groups;
}

/** The lanes of a view.
 * - `auto`: one plot with a left and a right axis for one or two unit groups, else one lane per group.
 * - `overlay`: one plot; the first two groups get the axes, later ones are scaled without an axis.
 * - `lanes`: one lane per series.
 * - Any `norm` but `raw`: one dimensionless axis labelled with the mode (per lane under `lanes`). */
export function axisLanes(series: AxisSeries[], layout: Layout, norm: Norm): LaneSpec[] {
  if (series.length === 0) return [];
  const groups = norm === "raw" ? unitGroups(series) : [{ unit: null, label: NORM_AXIS_LABEL[norm], ids: series.map((s) => s.id) }];
  const groupOf = (id: string) => groups.findIndex((g) => g.ids.includes(id));
  if (layout === "lanes") {
    return series.map((s) => ({
      key: `series-${s.id}`,
      axes: [{ scale: "y0", label: groups[groupOf(s.id)]!.label, side: "left" as const }],
      series: [{ id: s.id, scale: "y0" }],
    }));
  }
  if (layout === "overlay" || groups.length <= 2) {
    const sides: AxisSide[] = ["left", "right"];
    return [
      {
        key: "plot",
        axes: groups.map((g, i) => ({ scale: `y${i}`, label: g.label, side: sides[i] ?? "none" })),
        series: series.map((s) => ({ id: s.id, scale: `y${groupOf(s.id)}` })),
      },
    ];
  }
  return groups.map((g, i) => ({
    key: `unit-${i}`,
    axes: [{ scale: "y0", label: g.label, side: "left" as const }],
    series: g.ids.map((id) => ({ id, scale: "y0" })),
  }));
}
