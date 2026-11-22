import { describe, expect, it } from "vitest";
import { queryOfState, searchOfState, stateOfQuery, stateOfSearch, validateCompareSearch, type CompareState } from "../compare/url";
import { FINDING_ID, GROUP_ID, H, IDS, T0 } from "./compareFake";

const FULL: CompareState = {
  series: [IDS[2]!, IDS[0]!, IDS[1]!],
  range: { from: T0, to: T0 + 24 * H },
  layout: "lanes",
  norm: "index",
  group: GROUP_ID,
  finding: FINDING_ID,
};

describe("compare URL state", () => {
  it("url_state_round_trip: series order, window, layout, norm, group and finding survive", () => {
    const query = queryOfState(FULL);
    expect(query).toBe(
      `series=${IDS[2]},${IDS[0]},${IDS[1]}&from=${T0}&to=${T0 + 24 * H}&layout=lanes&norm=index&group=${GROUP_ID}&finding=${FINDING_ID}`,
    );
    expect(stateOfQuery(query)).toEqual(FULL);
    expect(stateOfQuery(`?${query}`)).toEqual(FULL);
    expect(stateOfSearch(searchOfState(FULL))).toEqual(FULL);
  });

  it("url_state_round_trip: defaults are left out and come back", () => {
    const minimal: CompareState = { series: [IDS[0]!, IDS[1]!], range: null, layout: "auto", norm: "raw" };
    expect(queryOfState(minimal)).toBe(`series=${IDS[0]},${IDS[1]}`);
    expect(stateOfQuery(queryOfState(minimal))).toEqual(minimal);
    expect(stateOfQuery("")).toEqual({ series: [], range: null, layout: "auto", norm: "raw" });
  });

  it("xy: the scatter's pair survives, after the finding, with a literal comma", () => {
    const state: CompareState = { ...FULL, xy: [IDS[1]!, IDS[2]!] };
    const query = queryOfState(state);
    expect(query.endsWith(`&finding=${FINDING_ID}&xy=${IDS[1]},${IDS[2]}`)).toBe(true);
    expect(stateOfQuery(query)).toEqual(state);
    expect(stateOfSearch(searchOfState(state))).toEqual(state);
    expect(Object.keys(searchOfState(state)).at(-1)).toBe("xy");
  });

  it("xy: dropped unless it is two different series of the view", () => {
    const series = `${IDS[0]},${IDS[1]}`;
    const xyOf = (xy: unknown) => validateCompareSearch({ series, xy }).xy;
    expect(xyOf(`${IDS[1]},${IDS[0]}`)).toBe(`${IDS[1]},${IDS[0]}`);
    expect(xyOf(` ${IDS[0]} , ${IDS[1]} `)).toBe(`${IDS[0]},${IDS[1]}`);
    expect(xyOf(`${IDS[0]},${IDS[2]}`)).toBeUndefined();
    expect(xyOf(`${IDS[0]},${IDS[0]}`)).toBeUndefined();
    expect(xyOf(IDS[0])).toBeUndefined();
    expect(xyOf(`${IDS[0]},${IDS[1]},${IDS[0]}`)).toBeUndefined();
    expect(xyOf("a,b")).toBeUndefined();
    expect(xyOf(7)).toBeUndefined();
    expect(validateCompareSearch({ xy: `${IDS[0]},${IDS[1]}` })).toEqual({});
    // A changed series list drops a pair that no longer fits.
    const state: CompareState = { series: [IDS[0]!, IDS[1]!], range: null, layout: "auto", norm: "raw", xy: [IDS[0]!, IDS[1]!] };
    expect(searchOfState({ ...state, series: [IDS[0]!, IDS[2]!] })).toEqual({ series: `${IDS[0]},${IDS[2]}` });
  });

  it("drops unknown keys and malformed values", () => {
    expect(
      validateCompareSearch({
        series: ` ${IDS[0]}, ${IDS[0]} ,,${IDS[1]},<script>`,
        from: "12",
        to: "5",
        layout: "grid",
        norm: "log",
        group: "nope",
        finding: 7,
        extra: "x",
      }),
    ).toEqual({ series: `${IDS[0]},${IDS[1]}` });
    // The router parses bare digits as numbers.
    expect(validateCompareSearch({ from: T0, to: T0 + H })).toEqual({ from: String(T0), to: String(T0 + H) });
  });
});
