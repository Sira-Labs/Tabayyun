import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import type { RunStatus } from "../types";

export const TERMINAL_STATUSES: ReadonlySet<RunStatus> = new Set(["succeeded", "failed"]);
export const POLL_INTERVAL_MS = 2000;

/** The run, refetched every `intervalMs` while it is queued or running, then left alone. */
export function useRunPolling(runId: string, intervalMs: number = POLL_INTERVAL_MS) {
  return useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.getRun(runId),
    refetchInterval: (q) => {
      const status = q.state.data?.status;
      return status && TERMINAL_STATUSES.has(status) ? false : intervalMs;
    },
    refetchIntervalInBackground: false,
  });
}
