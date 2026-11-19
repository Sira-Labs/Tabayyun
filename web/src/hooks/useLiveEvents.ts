// The live event stream of the current workspace (spec 030): when the API says that a run
// finished or findings were stored, the queries that show them are reloaded.
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

/** First key segments of the queries a finished run can change. */
const RUN_QUERIES = ["runs", "run", "series", "scores", "suites"] as const;
/** First key segments of the queries new findings can change. */
const FINDING_QUERIES = ["findings", "series"] as const;
const ALL_QUERIES = [...new Set<string>([...RUN_QUERIES, ...FINDING_QUERIES])];

/** Timing of the stream's client; an object, so tests can shorten it.
 * `gatherMs`: reloads asked for within this time become one.
 * `reopenMs`: wait before opening a stream again that the server refused (the browser only
 * reconnects by itself after a stream that was open). */
export const live = { gatherMs: 1000, reopenMs: 30_000 };

/** The URL of a workspace's stream. `EventSource` cannot send the workspace header. */
export function eventsUrl(workspaceId: string): string {
  return `/api/events?workspace=${encodeURIComponent(workspaceId)}`;
}

/** Open the stream of `workspaceId` and reload `client`'s queries as its events say; returns
 * what closes it. After the stream was lost and is back, and on `resync`, everything a
 * missed event could have changed is reloaded. */
export function connectLiveEvents(client: QueryClient, workspaceId: string): () => void {
  const pending = new Set<string>();
  let gather: ReturnType<typeof setTimeout> | undefined;
  let reopen: ReturnType<typeof setTimeout> | undefined;
  let source: EventSource | undefined;
  let lost = false;

  const flush = () => {
    gather = undefined;
    const roots = new Set(pending);
    pending.clear();
    void client.invalidateQueries({ predicate: (query) => roots.has(String(query.queryKey[0])) });
  };
  const reload = (roots: readonly string[]) => {
    for (const root of roots) pending.add(root);
    gather ??= setTimeout(flush, live.gatherMs);
  };
  const open = () => {
    reopen = undefined;
    const stream = new EventSource(eventsUrl(workspaceId));
    source = stream;
    stream.onopen = () => {
      if (lost) reload(ALL_QUERIES);
      lost = false;
    };
    stream.onerror = () => {
      lost = true;
      // Closed for good (the server refused the stream): try again later. A stream that was
      // open and ended is reconnected by the browser, and stays CONNECTING meanwhile.
      if (stream.readyState === EventSource.CLOSED && reopen === undefined) {
        stream.close();
        reopen = setTimeout(open, live.reopenMs);
      }
    };
    stream.addEventListener("run.completed", () => reload(RUN_QUERIES));
    stream.addEventListener("finding.created", () => reload(FINDING_QUERIES));
    stream.addEventListener("resync", () => reload(ALL_QUERIES));
  };
  open();

  return () => {
    source?.close();
    clearTimeout(gather);
    clearTimeout(reopen);
  };
}

/** Keep the stream of `workspaceId` open while the component is mounted. Without a workspace
 * or without `EventSource` it does nothing, and the pages work as they do without it. */
export function useLiveEvents(workspaceId: string | null): void {
  const client = useQueryClient();
  useEffect(() => {
    if (workspaceId === null || typeof EventSource === "undefined") return;
    return connectLiveEvents(client, workspaceId);
  }, [client, workspaceId]);
}
