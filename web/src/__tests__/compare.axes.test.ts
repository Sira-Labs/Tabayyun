import { describe, expect, it } from "vitest";
import { NORM_AXIS_LABEL, axisLanes, unitGroups } from "../compare/axes";

const s = (id: string, unit: string | null) => ({ id, unit });

describe("compare axes", () => {
  it("axis_groups_by_unit: one unit is one plot with a left axis", () => {
    const lanes = axisLanes([s("a", "MW"), s("b", "MW")], "auto", "raw");
    expect(lanes).toHaveLength(1);
    expect(lanes[0]!.axes).toEqual([{ scale: "y0", label: "MW", side: "left" }]);
    expect(lanes[0]!.series).toEqual([
      { id: "a", scale: "y0" },
      { id: "b", scale: "y0" },
    ]);
  });

  it("axis_groups_by_unit: two units are one plot with a left and a right axis", () => {
    const lanes = axisLanes([s("a", "MW"), s("b", "kV"), s("c", "MW")], "auto", "raw");
    expect(lanes).toHaveLength(1);
    expect(lanes[0]!.axes).toEqual([
      { scale: "y0", label: "MW", side: "left" },
      { scale: "y1", label: "kV", side: "right" },
    ]);
    expect(lanes[0]!.series.map((x) => x.scale)).toEqual(["y0", "y1", "y0"]);
  });

  it("axis_groups_by_unit: three units are stacked lanes, one per unit group", () => {
    const lanes = axisLanes([s("a", "MW"), s("b", "kV"), s("c", "°C"), s("d", "kV")], "auto", "raw");
    expect(lanes.map((l) => l.axes[0]!.label)).toEqual(["MW", "kV", "°C"]);
    expect(lanes.map((l) => l.series.map((x) => x.id))).toEqual([["a"], ["b", "d"], ["c"]]);
    expect(lanes.every((l) => l.axes.length === 1 && l.axes[0]!.side === "left")).toBe(true);
  });

  it("puts series without a unit in a group of their own", () => {
    expect(unitGroups([s("a", null), s("b", "MW"), s("c", ""), s("d", " MW ")])).toEqual([
      { unit: null, label: "no unit", ids: ["a", "c"] },
      { unit: "MW", label: "MW", ids: ["b", "d"] },
    ]);
  });

  it("overlay forces one plot and lanes one lane per series", () => {
    const series = [s("a", "MW"), s("b", "kV"), s("c", "°C")];
    const overlay = axisLanes(series, "overlay", "raw");
    expect(overlay).toHaveLength(1);
    expect(overlay[0]!.axes.map((a) => a.side)).toEqual(["left", "right", "none"]);
    const lanes = axisLanes([s("a", "MW"), s("b", "MW")], "lanes", "raw");
    expect(lanes.map((l) => l.series.map((x) => x.id))).toEqual([["a"], ["b"]]);
  });

  it("puts every series on one dimensionless axis in a normalised mode", () => {
    const lanes = axisLanes([s("a", "MW"), s("b", "kV"), s("c", "°C")], "auto", "z");
    expect(lanes).toHaveLength(1);
    expect(lanes[0]!.axes).toEqual([{ scale: "y0", label: NORM_AXIS_LABEL.z, side: "left" }]);
    expect(new Set(lanes[0]!.series.map((x) => x.scale))).toEqual(new Set(["y0"]));
  });

  it("has no lanes without series", () => {
    expect(axisLanes([], "auto", "raw")).toEqual([]);
  });
});
