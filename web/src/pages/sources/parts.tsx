// Building blocks of the sources pages (spec 024): badges, following a job, a job's outcome.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { formatLocalTime } from "../../format";
import { isActive, sourcesApi, type HealthStatus, type JobDetail, type JobStatus } from "../../sources";

const HEALTH_CLASS: Record<HealthStatus, string> = {
  ok: "bg-emerald-100 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-200",
  degraded: "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200",
  failing: "bg-red-100 text-red-900 dark:bg-red-950 dark:text-red-200",
  unknown: "bg-slate-100 text-slate-800 dark:bg-slate-800 dark:text-slate-200",
};

const JOB_CLASS: Record<JobStatus, string> = {
  queued: "bg-slate-100 text-slate-800 dark:bg-slate-800 dark:text-slate-200",
  running: "bg-sky-100 text-sky-900 dark:bg-sky-950 dark:text-sky-200",
  succeeded: "bg-emerald-100 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-200",
  partial: "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200",
  failed: "bg-red-100 text-red-900 dark:bg-red-950 dark:text-red-200",
};

/** A source's health as a coloured label. */
export function HealthBadge({ status }: { status: HealthStatus | undefined }) {
  const s = status ?? "unknown";
  return <span className={`rounded px-2 py-0.5 text-xs font-medium ${HEALTH_CLASS[s] ?? HEALTH_CLASS.unknown}`}>{s}</span>;
}

/** A job's status as a coloured label. */
export function JobBadge({ status }: { status: JobStatus }) {
  return <span className={`rounded px-2 py-0.5 text-xs font-medium ${JOB_CLASS[status] ?? ""}`}>{status}</span>;
}

export const POLL_MS = 1500;

/** A source job, polled while it is queued or running; when it ends, the source and its
 * history are refreshed. */
export function useJob(sourceId: string, jobId: string | null) {
  const client = useQueryClient();
  const job = useQuery({
    queryKey: ["sources", sourceId, "job", jobId],
    queryFn: () => sourcesApi.job(sourceId, jobId as string),
    enabled: jobId !== null,
    refetchInterval: (q) => (q.state.data && !isActive(q.state.data.status) ? false : POLL_MS),
  });
  const done = job.data !== undefined && !isActive(job.data.status);
  useEffect(() => {
    if (done) {
      void client.invalidateQueries({ queryKey: ["sources", sourceId, "detail"] });
      void client.invalidateQueries({ queryKey: ["sources", sourceId, "jobs"] });
      void client.invalidateQueries({ queryKey: ["sources", "list"] });
    }
  }, [done, client, sourceId]);
  return job;
}

/** One line on how a job ended, for the action that started it. */
export function JobLine({ job }: { job: JobDetail | undefined }) {
  if (!job) return <p className="text-sm text-slate-600 dark:text-slate-400">Starting…</p>;
  return (
    <p className="flex flex-wrap items-center gap-2 text-sm" aria-live="polite">
      <JobBadge status={job.status} />
      {isActive(job.status) ? (
        <span>Waiting for the worker…</span>
      ) : (
        <span>
          {job.finished_at && formatLocalTime(job.finished_at)}
          {job.trigger !== "check" && job.trigger !== "search" && job.trigger !== "metadata" && ` · ${job.rows} rows`}
          {job.error && <span className="text-red-700 dark:text-red-400"> · {job.error}</span>}
        </span>
      )}
    </p>
  );
}
