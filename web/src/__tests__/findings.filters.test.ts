import { describe, expect, it } from "vitest";
import {
  bulkMessage,
  chunks,
  exportUrl,
  groupsUrl,
  hasFilters,
  inboxApi,
  listUrl,
  muteEnd,
  parseSearchString,
  searchString,
  selectionState,
  severitiesOf,
  statusParam,
  statusesOf,
  toggled,
  toggledAll,
  validateInboxSearch,
} from "../findingsInbox";
import { stubFetch } from "./helpers";

describe("findings.filters", () => {
  it("keeps known keys and values, in one spelling", () => {
    expect(validateInboxSearch({})).toEqual({});
    expect(
      validateInboxSearch({
        status: "resolved,open,bogus,open",
        severity: "low,critical",
        dimension: "validity",
        check_id: " tby.spikes ",
        assignee: "me",
        feedback: "false_positive",
        q: 12,
        group: "series",
        open: "abc",
        other: "x",
      }),
    ).toEqual({
      status: "open,resolved",
      severity: "critical,low",
      dimension: "validity",
      check_id: "tby.spikes",
      assignee: "me",
      feedback: "false_positive",
      q: "12",
      group: "series",
      open: "abc",
    });
    // The default statuses and "every severity" are the absence of the key.
    expect(validateInboxSearch({ status: "acked,open", severity: "low,medium,high,critical" })).toEqual({});
    expect(validateInboxSearch({ status: "open,acked,muted,resolved" })).toEqual({ status: "all" });
    expect(validateInboxSearch({ status: "all" })).toEqual({ status: "all" });
    expect(validateInboxSearch({ dimension: "nope", feedback: "yes", assignee: "a b", group: "run", open: "k", q: "  " })).toEqual({});
    expect(validateInboxSearch({ q: "x".repeat(300) }).q).toHaveLength(200);
    // The group without an asset has the empty key.
    expect(validateInboxSearch({ group: "asset", open: "" })).toEqual({ group: "asset", open: "" });
  });

  it("reads and writes the multi-value filters", () => {
    expect(statusesOf({})).toEqual(["open", "acked"]);
    expect(statusesOf({ status: "all" })).toEqual(["open", "acked", "muted", "resolved"]);
    expect(statusesOf({ status: "muted" })).toEqual(["muted"]);
    expect(statusParam(["muted", "open"])).toBe("open,muted");
    expect(statusParam(["acked", "open"])).toBeUndefined();
    expect(statusParam([])).toBeUndefined();
    expect(statusParam(["open", "acked", "muted", "resolved"])).toBe("all");
    expect(severitiesOf({})).toEqual([]);
    expect(severitiesOf({ severity: "critical,high" })).toEqual(["critical", "high"]);
    expect(hasFilters({ group: "check", open: "k" })).toBe(false);
    expect(hasFilters({ q: "x" })).toBe(true);
  });

  it("builds the list, groups and export requests", () => {
    expect(listUrl({}, null)).toBe("/api/findings?limit=200");
    const search = { status: "open", severity: "critical,high", assignee: "me", q: "feeder 4", group: "series" as const, open: "s1" };
    expect(listUrl(search, null, "c1")).toBe("/api/findings?status=open&severity=critical%2Chigh&assignee=me&q=feeder+4&limit=200&cursor=c1");
    expect(listUrl({ check_id: "tby.gaps" }, { by: "check", key: "tby.spikes" })).toBe("/api/findings?check_id=tby.spikes&limit=200");
    expect(listUrl({}, { by: "series", key: "s1" })).toBe("/api/findings?series_id=s1&limit=200");
    expect(listUrl({}, { by: "asset", key: "" })).toBe("/api/findings?asset_path=&limit=200");
    expect(groupsUrl(search, "series")).toBe("/api/findings/groups?by=series&status=open&severity=critical%2Chigh&assignee=me&q=feeder+4");
    expect(groupsUrl({}, "asset")).toBe("/api/findings/groups?by=asset");
    expect(exportUrl(search)).toBe("/api/findings/export?status=open&severity=critical%2Chigh&assignee=me&q=feeder+4");
    expect(exportUrl({ group: "check" })).toBe("/api/findings/export");
  });

  it("stores a search as a string and reads it back, without the open group", () => {
    const search = { status: "open", severity: "critical", group: "series" as const, open: "s1" };
    expect(searchString(search)).toBe("status=open&severity=critical&group=series");
    expect(parseSearchString("status=open&severity=critical&group=series&open=s1")).toEqual({ status: "open", severity: "critical", group: "series" });
    expect(parseSearchString(searchString({ q: "a&b=c", feedback: "none" }))).toEqual({ q: "a&b=c", feedback: "none" });
    expect(searchString({})).toBe("");
    expect(parseSearchString("")).toEqual({});
  });

  it("selects rows and all loaded rows", () => {
    const ids = ["a", "b", "c"];
    expect([...toggled(new Set(["a"]), "b")]).toEqual(["a", "b"]);
    expect([...toggled(new Set(["a", "b"]), "a")]).toEqual(["b"]);
    expect(selectionState(ids, new Set())).toBe("none");
    expect(selectionState(ids, new Set(["b"]))).toBe("some");
    expect(selectionState(ids, new Set(ids))).toBe("all");
    expect(selectionState(ids, new Set(["gone"]))).toBe("none");
    expect(selectionState([], new Set(["a"]))).toBe("none");
    expect([...toggledAll(ids, new Set(["b"]))].sort()).toEqual(ids);
    expect(toggledAll(ids, new Set(ids)).size).toBe(0);
    // A selected id the list does not show is neither dropped nor counted.
    expect([...toggledAll(ids, new Set(["gone"]))].sort()).toEqual([...ids, "gone"].sort());
    expect([...toggledAll(ids, new Set([...ids, "gone"]))]).toEqual(["gone"]);
  });

  it("words a bulk result", () => {
    expect(bulkMessage("acknowledged", { updated: Array.from({ length: 12 }, (_, i) => `f${i}`), failed: [] })).toBe("12 findings acknowledged.");
    expect(bulkMessage("muted", { updated: ["a"], failed: [] })).toBe("1 finding muted.");
    expect(
      bulkMessage("acknowledged", {
        updated: ["a", "b"],
        failed: [
          { id: "c", error: "transition", status: "resolved" },
          { id: "d", error: "not_found", status: null },
          { id: "e", error: "transition", status: "resolved" },
        ],
      }),
    ).toBe("2 findings acknowledged. 3 could not be changed: 2 are resolved, 1 no longer exists.");
    expect(
      bulkMessage("resolved", {
        updated: [],
        failed: [
          { id: "c", error: "transition", status: "acked" },
          { id: "d", error: "not_found", status: null },
          { id: "e", error: "not_found", status: null },
          { id: "f", error: "transition", status: null },
        ],
      }),
    ).toBe("No findings resolved. 4 could not be changed: 1 is acknowledged, 2 no longer exist, 1 cannot make this change.");
  });

  it("turns the end of a mute into an instant, in the future and within a year", () => {
    const now = new Date("2026-11-17T08:00:00Z");
    expect(muteEnd("", now)).toEqual({ iso: null, error: null });
    // The test zone is UTC+03:00.
    expect(muteEnd("2026-11-20T14:00", now)).toEqual({ iso: "2026-11-20T11:00:00.000Z", error: null });
    expect(muteEnd("2026-11-17T10:59", now).error).toBe("The end must lie in the future.");
    expect(muteEnd("2027-11-19T12:00", now).error).toBe("The end can be at most 366 days ahead.");
    expect(muteEnd("soon", now).error).toBe("Enter a date and time.");
  });

  it("sends more than 500 ids in runs of 500 and merges the answers", async () => {
    expect(chunks([1, 2, 3, 4, 5], 2)).toEqual([[1, 2], [3, 4], [5]]);
    const sizes: number[] = [];
    stubFetch((url, init) => {
      if (url !== "/api/findings/bulk") throw new Error(`unexpected ${url}`);
      const { ids } = JSON.parse(String(init?.body)) as { ids: string[] };
      sizes.push(ids.length);
      return { updated: ids.slice(1), failed: [{ id: ids[0], error: "not_found", status: null }] };
    });
    const ids = Array.from({ length: 1201 }, (_, i) => `f-${i}`);
    const result = await inboxApi.bulk(ids, { status: "acked" });
    expect(sizes).toEqual([500, 500, 201]);
    expect(result.updated).toHaveLength(1198);
    expect(result.failed.map((f) => f.id)).toEqual(["f-0", "f-500", "f-1000"]);
  });
});
