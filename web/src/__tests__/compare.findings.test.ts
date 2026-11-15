import { describe, expect, it } from "vitest";
import { compareSearchOfFinding, compareSeriesOf, emphasisOf, isCrossSeries, suspectOf } from "../compare/findings";
import { FINDING_ID, GROUP_ID, H, IDS, T0, finding } from "./compareFake";

describe("compare findings", () => {
  it("finding_selection_marks_suspect: the evidence's suspect is emphasised, the rest muted", () => {
    const f = finding({ series_id: IDS[0]!, evidence: { group_id: GROUP_ID, members: IDS.slice(0, 4), suspect: IDS[2] } });
    expect(suspectOf(f)).toBe(IDS[2]);
    expect(emphasisOf(f, IDS.slice(0, 4))).toEqual({ [IDS[0]!]: "muted", [IDS[1]!]: "muted", [IDS[2]!]: "suspect", [IDS[3]!]: "muted" });
  });

  it("finding_selection_marks_suspect: without a suspect the finding's own series is it", () => {
    const f = finding({ series_id: IDS[1]!, evidence: { group_id: GROUP_ID, members: IDS.slice(0, 3), suspect: null } });
    expect(suspectOf(f)).toBe(IDS[1]);
    expect(emphasisOf(f, IDS.slice(0, 3))).toEqual({ [IDS[0]!]: "muted", [IDS[1]!]: "suspect", [IDS[2]!]: "muted" });
    expect(emphasisOf(null, IDS.slice(0, 3))).toEqual({});
    // A finding of a series that is not shown emphasises nothing.
    expect(emphasisOf(finding({ series_id: IDS[8]!, evidence: {} }), IDS.slice(0, 3))).toEqual({});
  });

  it("tells cross-series findings by group_id or partner", () => {
    expect(isCrossSeries(finding())).toBe(true);
    expect(isCrossSeries(finding({ evidence: { partner: IDS[1] } }))).toBe(true);
    expect(isCrossSeries(finding({ evidence: { gap_start: 1 } }))).toBe(false);
  });

  it("builds the compare link of a balance finding: members, group, finding and a window around it", () => {
    const f = finding();
    expect(compareSearchOfFinding(f)).toEqual({
      series: [IDS[2], IDS[0], IDS[1], IDS[3]].join(","),
      from: String(T0 + 28.5 * H),
      to: String(T0 + 37.5 * H),
      group: GROUP_ID,
      finding: FINDING_ID,
    });
  });

  it("builds the compare link of a correlation break: the pair", () => {
    const f = finding({ check_id: "tby.correlation_break", series_id: IDS[0]!, evidence: { group_id: GROUP_ID, members: IDS.slice(0, 3), partner: IDS[1] } });
    expect(compareSeriesOf(f)).toEqual([IDS[0], IDS[1], IDS[2]]);
    const pair = finding({ series_id: IDS[0]!, evidence: { partner: IDS[1] } });
    expect(compareSearchOfFinding(pair)?.series).toBe(`${IDS[0]},${IDS[1]}`);
    expect(compareSearchOfFinding(pair)?.group).toBeUndefined();
  });

  it("has no compare link for a single-series finding, and keeps the suspect within eight", () => {
    expect(compareSearchOfFinding(finding({ evidence: { gap_start: 1 } }))).toBeNull();
    const many = ["m1", "m2", "m3", "m4", "m5", "m6", "m7", "m8", "m9"];
    const kept = compareSeriesOf(finding({ series_id: "m1", evidence: { group_id: GROUP_ID, members: many, suspect: "m9" } }));
    expect(kept).toHaveLength(8);
    expect(kept[0]).toBe("m1");
    expect(kept).toContain("m9");
  });
});
