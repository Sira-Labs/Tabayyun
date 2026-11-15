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
