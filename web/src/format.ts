const NS_PER_MS = 1_000_000;

export function formatTs(ns: number): string {
  return new Date(ns / NS_PER_MS).toISOString().replace("T", " ").replace(".000Z", "Z");
}

export function formatDuration(ns: number): string {
  const s = Math.round(ns / 1e9);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h${m % 60 ? ` ${m % 60}m` : ""}`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

export const severityClass: Record<string, string> = {
  critical: "bg-red-600 text-white",
  high: "bg-orange-500 text-white",
  medium: "bg-amber-400 text-slate-900",
  low: "bg-slate-300 text-slate-900 dark:bg-slate-600 dark:text-slate-100",
};
