import { describe, expect, it } from "vitest";
import { SCHEDULE_PRESETS, describeCron, hasFiveFields } from "../suiteSchedule";

describe("describeCron", () => {
  it.each([
    ["5 * * * *", "Every hour at :05"],
    ["0 6 * * *", "Every day at 06:00 UTC"],
    ["*/15 * * * *", "Every 15 minutes"],
    ["* * * * *", "Every minute"],
    ["30 7 * * 1", "Mondays at 07:30 UTC"],
    ["30 7 * * MON", "Mondays at 07:30 UTC"],
    ["0 18 * * 7", "Sundays at 18:00 UTC"],
    ["0 8 * * 1-5", "Weekdays at 08:00 UTC"],
    ["0 8 * * mon-fri", "Weekdays at 08:00 UTC"],
    ["0 9 * * 6,0", "Weekends at 09:00 UTC"],
    ["15 6 * * 1,3,5", "Mondays, Wednesdays and Fridays at 06:15 UTC"],
    ["5 */6 * * *", "Every 6 hours at :05"],
    ["0 */1 * * *", "Every hour at :00"],
    ["0 2 1 * *", "Day 1 of every month at 02:00 UTC"],
    ["  0   6 * * *  ", "Every day at 06:00 UTC"],
  ])("puts %s into words", (expr, words) => {
    expect(describeCron(expr)).toBe(words);
  });

  it.each(["0 6 1 1 *", "0,30 * * * *", "0 6 1 * 1", "0 6 * * 2-4", "61 * * * *", "0 25 * * *", "0 6 * *", "@hourly"])(
    "shows %s as it is",
    (expr) => {
      expect(describeCron(expr)).toBe(expr);
    },
  );

  it("calls no schedule 'Only on request'", () => {
    expect(describeCron(null)).toBe("Only on request");
    expect(describeCron(undefined)).toBe("Only on request");
    expect(describeCron("  ")).toBe("Only on request");
  });
});

describe("schedule presets", () => {
  it("offers hourly, six-hourly, daily, weekly and only on request", () => {
    expect(SCHEDULE_PRESETS.map((p) => p.label)).toEqual(["Hourly", "Every 6 hours", "Daily", "Weekly", "Only on request"]);
    expect(SCHEDULE_PRESETS.map((p) => describeCron(p.cron))).toEqual([
      "Every hour at :05",
      "Every 6 hours at :05",
      "Every day at 06:00 UTC",
      "Mondays at 06:00 UTC",
      "Only on request",
    ]);
  });

  it("has presets with five fields, or none", () => {
    for (const preset of SCHEDULE_PRESETS) expect(preset.cron === "" || hasFiveFields(preset.cron)).toBe(true);
  });
});

describe("hasFiveFields", () => {
  it("counts fields, whatever the spacing", () => {
    expect(hasFiveFields("5 * * * *")).toBe(true);
    expect(hasFiveFields(" 5  *\t* * * ")).toBe(true);
    expect(hasFiveFields("5 * * *")).toBe(false);
    expect(hasFiveFields("0 5 * * * *")).toBe(false);
    expect(hasFiveFields("")).toBe(false);
  });
});
