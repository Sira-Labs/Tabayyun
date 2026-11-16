// Building blocks of the suites pages (spec 027).
import { Link } from "@tanstack/react-router";
import { StatusBadge } from "../../components/Badges";
import { formatLocalTime } from "../../format";
import { describeCron } from "../../suiteSchedule";
import type { Suite, ThresholdSource } from "../../suites";

export const primaryLink =
  "min-h-11 rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white dark:bg-slate-100 dark:text-slate-900";
export const mutedClass = "text-slate-600 dark:text-slate-400";

const SOURCE_CLASS: Record<ThresholdSource, string> = {
  override: "bg-sky-100 text-sky-900 dark:bg-sky-950 dark:text-sky-200",
  default: "bg-slate-100 text-slate-800 dark:bg-slate-800 dark:text-slate-200",
  metadata: "bg-emerald-100 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-200",
  baseline: "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200",
};

/** Where a threshold's value comes from, as a coloured label. */
export function SourceBadge({ source }: { source: ThresholdSource }) {
  return <span className={`rounded px-2 py-0.5 text-xs font-medium ${SOURCE_CLASS[source] ?? ""}`}>{source}</span>;
}

/** A suite's dataset by name, or that it is gone (spec 027, behaviour 6). */
export function DatasetName({ suite }: { suite: Suite }) {
  if (suite.dataset_id === null) return <span className="text-amber-800 dark:text-amber-300">dataset deleted</span>;
  return <>{suite.dataset_name ?? suite.dataset_id}</>;
}

/** A suite's schedule in words, with the expression as a tooltip. */
export function ScheduleText({ schedule }: { schedule: string | null }) {
  return <span title={schedule ?? undefined}>{describeCron(schedule)}</span>;
}

/** A run's status and start as a link to its report. */
export function RunLink({ run }: { run: { id: string; status: string; created_at: string } }) {
  return (
    <Link to="/runs/$runId" params={{ runId: run.id }} className="inline-flex flex-wrap items-center gap-2 underline-offset-2 hover:underline">
      <StatusBadge status={run.status} /> {formatLocalTime(run.created_at)}
    </Link>
  );
}

/** An error under the form field it belongs to, or nothing. */
export function FieldError({ id, message }: { id: string; message: string | null | undefined }) {
  if (!message) return null;
  return (
    <span id={id} role="alert" className="text-sm text-red-700 dark:text-red-400">
      {message}
    </span>
  );
}
