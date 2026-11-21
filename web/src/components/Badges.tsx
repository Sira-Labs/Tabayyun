import { severityClass, statusClass } from "../format";

/** Run status as a coloured label. */
export function StatusBadge({ status }: { status: string }) {
  return <span className={`rounded px-2 py-0.5 text-xs font-medium ${statusClass[status] ?? ""}`}>{status}</span>;
}

/** Finding severity as a coloured label. */
export function SeverityBadge({ severity }: { severity: string }) {
  return <span className={`rounded px-2 py-0.5 text-xs font-medium ${severityClass[severity] ?? ""}`}>{severity}</span>;
}

/** The text colour of a score (0–100): good from 90, fair from 70, poor below. */
export function scoreTone(value: number): string {
  return value >= 90 ? "text-emerald-700 dark:text-emerald-400" : value >= 70 ? "text-amber-700 dark:text-amber-400" : "text-red-700 dark:text-red-400";
}

/** One score (0–100) with a tone for good, fair and poor. */
export function ScoreTile({ label, value }: { label: string; value: number }) {
  const tone = scoreTone(value);
  return (
    <div className="rounded-md border border-slate-200 bg-white p-3 dark:border-slate-800 dark:bg-slate-900">
      <div className="text-xs uppercase tracking-wide text-slate-600 dark:text-slate-400">{label}</div>
      <div className={`text-2xl font-semibold tabular-nums ${tone}`}>{value.toFixed(1)}</div>
    </div>
  );
}
