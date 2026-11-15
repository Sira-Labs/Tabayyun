// A suite's schedule (spec 027): five-field cron in UTC, put into words, and the form's presets.
// The API is the authority on what parses; this only describes the common shapes.

/** A schedule the form offers with one click; an empty `cron` means "only on request". */
export type SchedulePreset = { label: string; cron: string };

export const SCHEDULE_PRESETS: readonly SchedulePreset[] = [
  { label: "Hourly", cron: "5 * * * *" },
  { label: "Every 6 hours", cron: "5 */6 * * *" },
  { label: "Daily", cron: "0 6 * * *" },
  { label: "Weekly", cron: "0 6 * * 1" },
  { label: "Only on request", cron: "" },
];

const DAY_NAMES = ["Sundays", "Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays"] as const;
const DAY_ABBREVIATIONS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"];

/** The fields of an expression, split on white space. */
function fieldsOf(expr: string): string[] {
  const text = expr.trim();
  return text ? text.split(/\s+/) : [];
}

/** Whether an expression has the five cron fields (minute, hour, day, month, weekday). */
export function hasFiveFields(expr: string): boolean {
  return fieldsOf(expr).length === 5;
}

/** A field that is one whole number within its range, or null. */
function int(field: string, min: number, max: number): number | null {
  if (!/^\d+$/.test(field)) return null;
  const n = Number(field);
  return n >= min && n <= max ? n : null;
}

// The step of a "star slash n" field when n divides `cycle` (60 minutes, 24 hours). Cron
// restarts a step at the top of the hour or day, so only then are the firings evenly n apart:
// a step of 7 hours fires at 21:00 and again at 00:00.
function stepOf(field: string, cycle: number): number | null {
  const match = /^\*\/(\d+)$/.exec(field);
  const n = match ? Number(match[1]) : 0;
  return n >= 1 && cycle % n === 0 ? n : null;
}

/** One weekday as 0 (Sunday) to 6, from a number (7 is Sunday too) or a three-letter name. */
function dayOf(part: string): number | null {
  const named = DAY_ABBREVIATIONS.indexOf(part.toLowerCase());
  if (named >= 0) return named;
  const n = int(part, 0, 7);
  return n === null ? null : n % 7;
}

/** A weekday field in words ("Weekdays", "Mondays and Thursdays"), or null. */
function daysOf(field: string): string | null {
  const range = /^([^-,]+)-([^-,]+)$/.exec(field);
  if (range) {
    const from = dayOf(range[1] as string);
    const to = dayOf(range[2] as string);
    if (from === 1 && to === 5) return "Weekdays";
    return null;
  }
  const days: number[] = [];
  for (const part of field.split(",")) {
    const day = dayOf(part);
    if (day === null) return null;
    if (!days.includes(day)) days.push(day);
  }
  if (days.length === 2 && days.includes(0) && days.includes(6)) return "Weekends";
  const names = days.map((d) => DAY_NAMES[d] as string);
  if (names.length <= 1) return names[0] ?? null;
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

const pad = (n: number) => String(n).padStart(2, "0");

/** A five-field cron expression in words, in UTC; an empty one is "Only on request", and one
 * this does not know is shown as it is. */
export function describeCron(expr: string | null | undefined): string {
  const text = (expr ?? "").trim();
  if (!text) return "Only on request";
  const fields = fieldsOf(text);
  if (fields.length !== 5) return text;
  const [minute, hour, dayOfMonth, month, weekday] = fields as [string, string, string, string, string];
  if (month !== "*") return text;
  const everyDay = dayOfMonth === "*" && weekday === "*";
  const m = int(minute, 0, 59);

  if (everyDay && hour === "*") {
    const step = minute === "*" ? 1 : stepOf(minute, 60);
    if (step !== null) return step === 1 ? "Every minute" : `Every ${step} minutes`;
    return m === null ? text : `Every hour at :${pad(m)}`;
  }
  if (m === null) return text;
  if (everyDay) {
    const step = stepOf(hour, 24);
    if (step !== null) return step === 1 ? `Every hour at :${pad(m)}` : `Every ${step} hours at :${pad(m)}`;
  }
  const h = int(hour, 0, 23);
  if (h === null) return text;
  const at = `at ${pad(h)}:${pad(m)} UTC`;
  if (everyDay) return `Every day ${at}`;
  if (dayOfMonth === "*") {
    const days = daysOf(weekday);
    return days ? `${days} ${at}` : text;
  }
  if (weekday === "*") {
    const day = int(dayOfMonth, 1, 31);
    return day === null ? text : `Day ${day} of every month ${at}`;
  }
  return text;
}
