import { useInfiniteQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { api } from "../api";
import { StatusBadge } from "../components/Badges";
import { TERMINAL_STATUSES } from "../hooks/useRunPolling";
import { formatDuration, formatLocalTime } from "../format";
import type { Run } from "../types";

/** Run duration as text, or a dash while it has none. */
function duration(run: Run): string {
  return run.duration_ms === null ? "—" : formatDuration(run.duration_ms * 1_000_000);
}

/** `/runs`: recent runs, newest first, with "load more". */
export function RunsList() {
  const runs = useInfiniteQuery({
    queryKey: ["runs"],
    queryFn: ({ pageParam }) => api.listRuns(pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    // Keep the list moving while something is still queued or running.
    refetchInterval: (q) =>
      q.state.data?.pages.some((p) => p.items.some((r) => !TERMINAL_STATUSES.has(r.status))) ? 5000 : false,
  });
  const items = runs.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <section aria-labelledby="runs-heading" className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 id="runs-heading" className="text-lg font-semibold">
          Runs
        </h1>
        <Link to="/runs/new" className="min-h-11 rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white dark:bg-slate-100 dark:text-slate-900">
          New run
        </Link>
      </div>
      {runs.isPending && <p>Loading runs…</p>}
      {runs.isError && (
        <p role="alert" className="text-red-700 dark:text-red-400">
          Could not load runs: {runs.error.message}
        </p>
      )}
      {runs.isSuccess && items.length === 0 && (
        <p className="text-slate-600 dark:text-slate-400">
          No runs yet. <Link to="/runs/new" className="underline">Upload a CSV</Link> to start one.
        </p>
      )}
      {items.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">Runs, newest first</caption>
            <thead className="bg-slate-100 text-xs uppercase tracking-wide text-slate-700 dark:bg-slate-800 dark:text-slate-300">
              <tr>
                <th scope="col" className="p-2">Status</th>
                <th scope="col" className="p-2">Series</th>
                <th scope="col" className="p-2">Started</th>
                <th scope="col" className="hidden p-2 text-right sm:table-cell">Duration</th>
                <th scope="col" className="hidden p-2 text-right sm:table-cell">Findings</th>
                <th scope="col" className="p-2 text-right">Score</th>
              </tr>
            </thead>
            <tbody>
              {items.map((run) => (
                <tr key={run.id} className="border-t border-slate-200 dark:border-slate-800">
                  <td className="p-2">
                    <StatusBadge status={run.status} />
                  </td>
                  <td className="p-2">
                    <Link to="/runs/$runId" params={{ runId: run.id }} className="font-medium underline-offset-2 hover:underline">
                      {run.series[0]?.external_id ?? "run"}
                    </Link>
                  </td>
                  <td className="p-2 text-xs sm:whitespace-nowrap">{formatLocalTime(run.started_at ?? run.created_at)}</td>
                  <td className="hidden p-2 text-right tabular-nums sm:table-cell">{duration(run)}</td>
                  <td className="hidden p-2 text-right tabular-nums sm:table-cell">{run.stats.n_findings ?? "—"}</td>
                  <td className="p-2 text-right tabular-nums">{run.series[0] ? run.series[0].score.toFixed(1) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {runs.hasNextPage && (
        <button
          type="button"
          onClick={() => runs.fetchNextPage()}
          disabled={runs.isFetchingNextPage}
          className="min-h-11 rounded border border-slate-300 px-4 py-2 text-sm font-medium disabled:opacity-50 dark:border-slate-700"
        >
          {runs.isFetchingNextPage ? "Loading…" : "Load more"}
        </button>
      )}
    </section>
  );
}
