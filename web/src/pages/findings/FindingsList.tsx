import { useInfiniteQuery } from "@tanstack/react-query";
import { useCallback, useMemo, useRef, useState } from "react";
import { formatNumber } from "../../format";
import { inboxApi, inboxKeys, toggled, toggledAll, type GroupFilter, type InboxSearch } from "../../findingsInbox";
import { ErrorLine, linkButtonClass } from "../admin/ui";
import { BulkActions, type BulkDone } from "./BulkActions";
import { FindingsTable } from "./FindingsTable";
import { mutedClass } from "./parts";

/** The findings of a search, or of one of its groups: pages of 200 loaded while scrolling,
 * the row selection and, for editors, the actions on it (spec 029). The selection lives here,
 * so the page mounts a new list (by `key`) when the filter changes and the selection is gone. */
export function FindingsList({
  search,
  group,
  label,
  canWrite,
  onNotice,
}: {
  search: InboxSearch;
  group: GroupFilter | null;
  label: string;
  canWrite: boolean;
  onNotice: (message: string) => void;
}) {
  const frame = useRef<HTMLDivElement>(null);
  const [selected, setSelected] = useState<ReadonlySet<string>>(() => new Set());
  const findings = useInfiniteQuery({
    queryKey: inboxKeys.list(search, group),
    queryFn: ({ pageParam }) => inboxApi.list(search, group, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const items = useMemo(() => findings.data?.pages.flatMap((p) => p.items) ?? [], [findings.data]);
  const ids = useMemo(() => items.map((f) => f.id), [items]);
  const { fetchNextPage, hasNextPage } = findings;
  const loadMore = useCallback(() => void fetchNextPage(), [fetchNextPage]);
  // Findings the pruning below leaves alone, also when the list no longer shows them: those
  // of a change that is running, until its result is reported, and after it those the result
  // says stay selected (no answer, or not sent), until the next change, so trying again
  // tells what became of them.
  const [unknown, setUnknown] = useState<ReadonlySet<string>>(() => new Set());
  // Findings a reload dropped are no longer selected, also when a later reload brings them
  // back: the stored selection is pruned whenever the loaded findings change.
  const [pruned, setPruned] = useState(ids);
  if (ids !== pruned) {
    setPruned(ids);
    const kept = new Set([...ids, ...unknown]);
    if ([...selected].some((id) => !kept.has(id))) setSelected(new Set([...selected].filter((id) => kept.has(id))));
  }
  const chosen = useMemo(() => {
    const loaded = new Set(ids);
    return [...ids.filter((id) => selected.has(id)), ...[...unknown].filter((id) => selected.has(id) && !loaded.has(id))];
  }, [ids, selected, unknown]);

  // Only what the change was sent for leaves the selection: a row ticked while the request
  // was running stays ticked.
  const done: BulkDone = (message, sent, keep = []) => {
    setUnknown(new Set(keep));
    const gone = new Set(sent);
    for (const id of keep) gone.delete(id);
    setSelected((now) => new Set([...now].filter((id) => !gone.has(id))));
    onNotice(message);
    // The actions bar goes with the selection: keep the focus in the list.
    frame.current?.focus();
  };

  return (
    <div ref={frame} tabIndex={-1} role="group" aria-label={label} className="space-y-2 outline-none">
      {findings.isPending && <p>Loading findings…</p>}
      {!findings.isFetchNextPageError && <ErrorLine error={findings.error} prefix="Could not load findings" />}
      {findings.isSuccess && items.length === 0 && <p className={mutedClass}>No findings match.</p>}
      {canWrite && chosen.length > 0 && <BulkActions ids={chosen} onDone={done} onHold={(held) => setUnknown(new Set(held))} />}
      {items.length > 0 && (
        <>
          <FindingsTable
            items={items}
            label={label}
            hasMore={hasNextPage}
            autoLoad={!findings.isFetching && !findings.isFetchNextPageError}
            onLoadMore={loadMore}
            selection={
              canWrite
                ? {
                    selected,
                    onToggle: (id) => setSelected((now) => toggled(now, id)),
                    onToggleAll: () => setSelected((now) => toggledAll(ids, now)),
                  }
                : null
            }
          />
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <p className={mutedClass}>{hasNextPage ? `${formatNumber(items.length)} loaded` : `All ${formatNumber(items.length)} loaded`}</p>
            {hasNextPage && (
              <button type="button" className={linkButtonClass} disabled={findings.isFetchingNextPage} onClick={loadMore}>
                {findings.isFetchingNextPage ? "Loading more…" : "Load more"}
              </button>
            )}
          </div>
          {findings.isFetchNextPageError && <ErrorLine error={findings.error} prefix="Could not load more findings" />}
        </>
      )}
    </div>
  );
}
