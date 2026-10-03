import { describe, expect, it } from "vitest";
import { axisTicks, formatSpan, formatStep, lanes } from "../timeline";
import type { Finding } from "../types";

const HOUR = 3_600e9;
const DAY = 24 * HOUR;
const ms = (iso: string) => new Date(iso).getTime() * 1e6;

describe("formatSpan", () => {
  it("uses the largest unit that reads naturally", () => {
    expect(formatSpan(17_420 * HOUR)).toBe("2.0 years");
    expect(formatSpan(8 * 365.25 * DAY)).toBe("8.0 years");
    expect(formatSpan(330 * DAY)).toBe("11 months");
    expect(formatSpan(14 * DAY)).toBe("14 days");
    expect(formatSpan(6 * HOUR)).toBe("6 hours");
    expect(formatSpan(45 * 60e9)).toBe("45 minutes");
    expect(formatSpan(30e9)).toBe("1 minute");
  });
});

describe("formatStep", () => {
  it("names common steps and spells out the rest", () => {
    expect(formatStep(HOUR)).toBe("hourly");
    expect(formatStep(DAY)).toBe("daily");
    expect(formatStep(60e9)).toBe("every minute");
    expect(formatStep(600e9)).toBe("every 10 minutes");
    expect(formatStep(2.5 * HOUR)).toBe("every 2.5 hours");
    expect(formatStep(1.004 * HOUR)).toBe("hourly");
    expect(formatStep(15e9)).toBe("every 15 seconds");
  });
});

describe("axisTicks", () => {
  const cases: [string, string, string][] = [
    ["2 hours", "2024-03-05T10:07:00", "2024-03-05T12:07:00"],
    ["3 days", "2024-03-05T10:07:00", "2024-03-08T10:07:00"],
    ["4 months", "2024-03-05T10:07:00", "2024-07-05T10:07:00"],
    ["2 years", "2016-07-01T00:00:00", "2018-06-26T19:00:00"],
    ["8 years", "2009-01-01T00:10:00", "2017-01-01T00:00:00"],
  ];
  it.each(cases)("gives 3 to 12 ordered ticks inside the window for %s", (_name, a, b) => {
    const ticks = axisTicks(ms(a), ms(b));
    expect(ticks.length).toBeGreaterThanOrEqual(3);
    expect(ticks.length).toBeLessThanOrEqual(12);
    for (const [i, t] of ticks.entries()) {
      expect(t.at).toBeGreaterThanOrEqual(0);
      expect(t.at).toBeLessThanOrEqual(1);
      if (i > 0) expect(t.ns).toBeGreaterThan(ticks[i - 1]!.ns);
    }
  });

  it("labels years on a multi-year window and months on a shorter one", () => {
    expect(axisTicks(ms("2009-01-01T00:10:00"), ms("2017-01-01T00:00:00")).map((t) => t.label)).toEqual([
      "2010",
      "2011",
      "2012",
      "2013",
      "2014",
      "2015",
      "2016",
      "2017",
    ]);
    expect(axisTicks(ms("2024-03-05T00:00:00"), ms("2024-07-05T00:00:00")).map((t) => t.label)).toEqual([
      "Apr 2024",
      "May 2024",
      "Jun 2024",
      "Jul 2024",
    ]);
  });

  it("is empty for an empty window", () => {
    expect(axisTicks(5, 5)).toEqual([]);
  });
});

function f(id: string, check: string, severity: Finding["severity"], series = "s1"): Finding {
  return {
    id,
    check_id: check,
    series_id: series,
    dimension: "validity",
    severity,
    window: { start: 0, end: HOUR },
    score_impact: 0,
    summary: id,
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

describe("lanes", () => {
  it("groups by check, most severe lane first, then by name", () => {
    const out = lanes([f("a", "tby.spikes", "medium"), f("b", "tby.flatline", "high"), f("c", "tby.spikes", "critical"), f("d", "tby.drift", "medium")]);
    expect(out.map((l) => [l.label, l.findings.length])).toEqual([
      ["tby.spikes", 2],
      ["tby.flatline", 1],
      ["tby.drift", 1],
    ]);
  });

  it("splits by series on a dataset run and names the series", () => {
    const out = lanes([f("a", "tby.corr", "high", "s1"), f("b", "tby.corr", "high", "s2")], { s1: "hufl", s2: "lufl" });
    expect(out.map((l) => l.label)).toEqual(["tby.corr · hufl", "tby.corr · lufl"]);
  });
});
