const NS_PER_MS = 1_000_000;

function pad(n: number, width = 2): string {
  return String(Math.trunc(Math.abs(n))).padStart(width, "0");
}

/** `YYYY-MM-DD HH:MM:SS UTC±HH:MM` in the browser's time zone. */
export function formatLocalTime(value: Date | string | number): string {
  const d = typeof value === "number" ? new Date(value / NS_PER_MS) : new Date(value);
  const offset = -d.getTimezoneOffset();
  const sign = offset >= 0 ? "+" : "-";
  const date = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  return `${date} ${time} UTC${sign}${pad(offset / 60)}:${pad(offset % 60)}`;
}

/** A duration given in ns, e.g. `2h 25m`. */
export function formatDuration(ns: number): string {
  if (ns < 1e9) {
    const ms = ns / NS_PER_MS;
    return ms >= 1 ? `${Math.round(ms)} ms` : `${Math.round(ns / 1e3)} µs`;
  }
  const s = Math.round(ns / 1e9);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m${s % 60 ? ` ${s % 60}s` : ""}`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h${m % 60 ? ` ${m % 60}m` : ""}`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

/** Numbers for people: grouped integers, four significant digits for fractions. */
export function formatNumber(v: number): string {
  if (!Number.isFinite(v)) return String(v);
  if (Number.isInteger(v)) return v.toLocaleString("en-US");
  if (Math.abs(v) >= 1000) return v.toLocaleString("en-US", { maximumFractionDigits: 1 });
  return v.toLocaleString("en-US", { maximumSignificantDigits: 4 });
}

export const severityClass: Record<string, string> = {
  critical: "bg-red-700 text-white",
  high: "bg-orange-700 text-white",
  medium: "bg-amber-300 text-slate-900",
  low: "bg-slate-300 text-slate-900 dark:bg-slate-600 dark:text-slate-100",
};

export const severityRank: Record<string, number> = { critical: 0, high: 1, medium: 2, low: 3 };

export const statusClass: Record<string, string> = {
  queued: "bg-slate-200 text-slate-900 dark:bg-slate-700 dark:text-slate-100",
  running: "bg-sky-700 text-white",
  succeeded: "bg-emerald-700 text-white",
  failed: "bg-red-700 text-white",
};
