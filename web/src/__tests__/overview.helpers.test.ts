import { describe, expect, it } from "vitest";
import {
  barSegments,
  dayTotal,
  dayWords,
  findingsWords,
  lastFetchAt,
  metricLabel,
  overviewKeys,
  overviewUrl,
  scoreText,
  seriesFindingsSearch,
  severityFindingsSearch,
  shownWords,
  sparklinePoints,
  trendWords,
} from "../overview";
import { validateInboxSearch } from "../findingsInbox";
import { findingDays } from "./overviewFake";

const trend = [
  { day: "2026-10-23", mean: 97.1, n: 800 },
  { day: "2026-11-05", mean: 98, n: 900 },
  { day: "2026-11-21", mean: 96.4, n: 950 },
];

describe("overview helpers", () => {
  it("builds the request with days always and the filters only when given", () => {
    expect(overviewUrl({})).toBe("/api/overview?days=30");
    expect(overviewUrl({ days: 7, asset: "plant/feeder 4", source: "s1" })).toBe("/api/overview?days=7&asset=plant%2Ffeeder+4&source=s1");
    expect(overviewKeys.get({ days: 90 })).toEqual(["overview", "/api/overview?days=90"]);
    expect(overviewKeys.all).toEqual(["overview"]);
  });

  it("words a trend, one point and none", () => {
    expect(trendWords(trend, 30)).toBe("from 97.1 to 96.4 over 30 days");
    expect(trendWords(trend.slice(0, 1), 7)).toBe("97.1 on 2026-10-23, the only scored day in 7 days");
    expect(trendWords([], 90)).toBe("no scores in the last 90 days");
  });

  it("labels metrics and scores", () => {
    expect(metricLabel("overall")).toBe("Overall");
    expect(metricLabel("plausibility")).toBe("Plausibility");
    expect(scoreText(96.44)).toBe("96.4");
    expect(scoreText(41)).toBe("41.0");
    expect(scoreText(null)).toBe("—");
  });

  it("says how many worst series are shown only when there are more", () => {
    expect(shownWords(50, 498)).toBe("50 of 498 shown");
    expect(shownWords(50, 1200)).toBe("50 of 1,200 shown");
    expect(shownWords(3, 3)).toBeNull();
  });

  it("scales the sparkline to the box, highest mean at the top", () => {
    expect(sparklinePoints(trend, 100, 20, 2)).toBe("0,11 50,2 100,18");
    expect(sparklinePoints(trend.slice(0, 1), 100, 20)).toBe("0,10 100,10");
    expect(sparklinePoints([{ day: "d1", mean: 90, n: 1 }, { day: "d2", mean: 90, n: 1 }], 100, 20)).toBe("0,10 100,10");
    expect(sparklinePoints([], 100, 20)).toBe("");
  });

  it("stacks a day's bar from low at the bottom to critical at the top", () => {
    const day = { critical: 1, high: 2, medium: 0, low: 1 };
    expect(dayTotal(day)).toBe(4);
    expect(barSegments(day, 8, 100)).toEqual([
      { severity: "low", y: 87.5, height: 12.5 },
      { severity: "high", y: 62.5, height: 25 },
      { severity: "critical", y: 50, height: 12.5 },
    ]);
    expect(barSegments(day, 0, 100)).toEqual([]);
  });

  it("words a day and the whole chart", () => {
    const days = findingDays(3, { 1: { high: 2, medium: 1 } });
    expect(dayWords(days[1] as (typeof days)[number])).toBe("2026-11-20: 2 high, 1 medium");
    expect(dayWords(days[0] as (typeof days)[number])).toBe("2026-11-19: none");
    expect(findingsWords(days)).toBe("New findings per day from 2026-11-19 to 2026-11-21: 3 in total, most on 2026-11-20 (3)");
    expect(findingsWords(findingDays(2))).toBe("New findings per day from 2026-11-20 to 2026-11-21: 0 in total");
  });

  it("reads a source's last fetch as a time or as the stored health's object", () => {
    expect(lastFetchAt({ last_fetch: "2026-11-21T05:00:00Z" })).toBe("2026-11-21T05:00:00Z");
    expect(lastFetchAt({ last_fetch: { finished_at: "2026-11-21T05:00:00Z" } })).toBe("2026-11-21T05:00:00Z");
    expect(lastFetchAt({ last_fetch: null })).toBeNull();
  });

  it("links into the inbox with searches the inbox keeps as they are", () => {
    const series = seriesFindingsSearch("s1");
    expect(validateInboxSearch(series)).toEqual(series);
    expect(series).toEqual({ group: "series", open: "s1" });
    const severity = severityFindingsSearch("high");
    expect(validateInboxSearch(severity)).toEqual({ severity: "high" });
  });
});
