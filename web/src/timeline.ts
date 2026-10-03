// Pure helpers for the run report's time overview (spec 020): span and sampling in words,
// axis ticks across a data window, and findings grouped into one lane per check. Times are ns
// since the epoch; ticks fall on the browser's local calendar, like `formatLocalTime`.

import { severityRank } from "./format";
import type { Finding } from "./types";

const NS = { second: 1e9, minute: 60e9, hour: 3_600e9, day: 86_400e9 } as const;
const DAYS_PER_YEAR = 365.25;
const DAYS_PER_MONTH = DAYS_PER_YEAR / 12;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** `n unit` with the unit singular for exactly one. */
function count(n: number, unit: string): string {
  return `${n} ${n === 1 ? unit : `${unit}s`}`;
}

/** Up to one decimal, without a trailing `.0` unless `keep` (so "2.0 years" stays). */
function decimal(v: number, keep = false): string {
  const r = Math.round(v * 10) / 10;
  return keep || !Number.isInteger(r) ? r.toFixed(1) : String(r);
}

/** How long a window lasts, in the largest unit that reads naturally: "2.0 years", "11 months". */
export function formatSpan(ns: number): string {
  const days = ns / NS.day;
  if (days >= DAYS_PER_YEAR) return `${decimal(days / DAYS_PER_YEAR, true)} years`;
  if (days >= 60) return count(Math.round(days / DAYS_PER_MONTH), "month");
  if (days >= 2) return count(Math.round(days), "day");
  if (ns >= 2 * NS.hour) return count(Math.round(ns / NS.hour), "hour");
  return count(Math.max(1, Math.round(ns / NS.minute)), "minute");
}

/** A sampling step: "hourly", "daily", "every 10 minutes", "every 2.5 hours". */
export function formatStep(ns: number): string {
  const named: [number, string][] = [
    [NS.second, "every second"],
    [NS.minute, "every minute"],
    [NS.hour, "hourly"],
    [NS.day, "daily"],
    [7 * NS.day, "weekly"],
  ];
  for (const [size, name] of named) {
    if (Math.abs(ns - size) / size < 0.01) return name;
  }
  if (ns < NS.second) return `every ${decimal(ns / 1e6)} ms`;
  if (ns < NS.minute) return `every ${decimal(ns / NS.second)} seconds`;
  if (ns < NS.hour) return `every ${decimal(ns / NS.minute)} minutes`;
  if (ns < NS.day) return `every ${decimal(ns / NS.hour)} hours`;
  return `every ${decimal(ns / NS.day)} days`;
}

export type Tick = { ns: number; at: number; label: string };

type Step = { unit: "minute" | "hour" | "day" | "month" | "year"; size: number };

// Smallest first: the first step that yields at most MAX_TICKS ticks is used.
const STEPS: Step[] = [
  { unit: "minute", size: 5 },
  { unit: "minute", size: 15 },
  { unit: "minute", size: 30 },
  { unit: "hour", size: 1 },
  { unit: "hour", size: 3 },
  { unit: "hour", size: 6 },
  { unit: "hour", size: 12 },
  { unit: "day", size: 1 },
  { unit: "day", size: 2 },
  { unit: "day", size: 7 },
  { unit: "month", size: 1 },
  { unit: "month", size: 3 },
  { unit: "month", size: 6 },
  { unit: "year", size: 1 },
  { unit: "year", size: 2 },
  { unit: "year", size: 5 },
  { unit: "year", size: 10 },
];
const MAX_TICKS = 12;

/** The first boundary of `step` at or after `d` (local calendar). */
function firstBoundary(d: Date, step: Step): Date {
  const b = new Date(d);
  b.setSeconds(0, 0);
  if (step.unit === "minute") {
    if (b < d) b.setMinutes(b.getMinutes() + 1);
    b.setMinutes(Math.ceil(b.getMinutes() / step.size) * step.size);
    return b;
  }
  b.setMinutes(0);
  if (step.unit === "hour") {
    if (b < d) b.setHours(b.getHours() + 1);
    b.setHours(Math.ceil(b.getHours() / step.size) * step.size);
    return b;
  }
  b.setHours(0);
  if (step.unit === "day") {
    if (b < d) b.setDate(b.getDate() + 1);
    return b;
  }
  b.setDate(1);
  if (step.unit === "month") {
    if (b < d) b.setMonth(b.getMonth() + 1);
    b.setMonth(Math.ceil(b.getMonth() / step.size) * step.size);
    return b;
  }
  b.setMonth(0);
  if (b < d) b.setFullYear(b.getFullYear() + 1);
  b.setFullYear(Math.ceil(b.getFullYear() / step.size) * step.size);
  return b;
}

function advance(d: Date, step: Step): Date {
  const n = new Date(d);
  if (step.unit === "minute") n.setMinutes(n.getMinutes() + step.size);
  else if (step.unit === "hour") n.setHours(n.getHours() + step.size);
  else if (step.unit === "day") n.setDate(n.getDate() + step.size);
  else if (step.unit === "month") n.setMonth(n.getMonth() + step.size);
  else n.setFullYear(n.getFullYear() + step.size);
  return n;
}

function label(d: Date, step: Step): string {
  const hhmm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  if (step.unit === "minute" || step.unit === "hour") return hhmm === "00:00" ? `${d.getDate()} ${MONTHS[d.getMonth()]}` : hhmm;
  if (step.unit === "day") return `${d.getDate()} ${MONTHS[d.getMonth()]}`;
  if (step.unit === "month") return d.getMonth() === 0 ? String(d.getFullYear()) : `${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
  return String(d.getFullYear());
}

/** Ticks on whole calendar units inside `[start, end]`, at most 12, each with its position. */
export function axisTicks(startNs: number, endNs: number): Tick[] {
  if (!(endNs > startNs)) return [];
  const start = new Date(startNs / 1e6);
  const end = new Date(endNs / 1e6);
  for (const step of STEPS) {
    const ticks: Tick[] = [];
    for (let d = firstBoundary(start, step); d <= end; d = advance(d, step)) {
      const ns = d.getTime() * 1e6;
      ticks.push({ ns, at: (ns - startNs) / (endNs - startNs), label: label(d, step) });
      if (ticks.length > MAX_TICKS) break;
    }
    if (ticks.length <= MAX_TICKS) return ticks;
  }
  return [];
}

export type Lane = { key: string; label: string; check: string; series: string | null; findings: Finding[]; worst: number };

/** Findings grouped by check (and by series on a dataset run), most severe lane first. */
export function lanes(findings: Finding[], seriesNames?: Record<string, string>): Lane[] {
  const multi = new Set(findings.map((f) => f.series_id)).size > 1;
  const byKey = new Map<string, Lane>();
  for (const f of findings) {
    const key = multi ? `${f.check_id}|${f.series_id}` : f.check_id;
    let lane = byKey.get(key);
    if (!lane) {
      const series = seriesNames?.[f.series_id] ?? f.series_id.slice(0, 8);
      lane = {
        key,
        label: multi ? `${f.check_id} · ${series}` : f.check_id,
        check: f.check_id,
        series: multi ? series : null,
        findings: [],
        worst: 9,
      };
      byKey.set(key, lane);
    }
    lane.findings.push(f);
    lane.worst = Math.min(lane.worst, severityRank[f.severity] ?? 9);
  }
  return [...byKey.values()].sort((a, b) => a.worst - b.worst || a.label.localeCompare(b.label));
}
