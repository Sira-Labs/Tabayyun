import { useQuery } from "@tanstack/react-query";
import { useId, type ReactNode } from "react";
import { formatLocalTime, formatNumber } from "../../format";
import { inboxApi, inboxKeys, type FindingGroup, type Grouping, type InboxSearch } from "../../findingsInbox";
import { ErrorLine } from "../admin/ui";
import { SeverityCounts, mutedClass } from "./parts";

const NOUN: Record<Grouping, string> = { check: "check", series: "series", asset: "asset" };

/** What a group is called: the API's label, else its key, else that it has none. */
export function groupLabel(by: Grouping, group: FindingGroup): string {
  return group.label || group.key || (by === "asset" ? "No asset" : "(none)");
}

/** The groups of a search by check, series or asset (spec 029): each with its count, its
 * severities and its newest window, largest first. One is open at a time; `children` draws the
 * findings of the open one. */
export function Groups({
  search,
  by,
  openKey,
  onOpen,
  children,
}: {
  search: InboxSearch;
  by: Grouping;
  openKey: string | undefined;
  onOpen: (key: string | undefined) => void;
  children: (group: FindingGroup) => ReactNode;
}) {
  const panelId = useId();
  const groups = useQuery({ queryKey: inboxKeys.groups(search, by), queryFn: () => inboxApi.groups(search, by) });
  const items = groups.data?.items ?? [];

  return (
    <div className="space-y-2">
      {groups.isPending && <p>Loading groups…</p>}
      <ErrorLine error={groups.error} prefix="Could not load the groups" />
      {groups.isSuccess && items.length === 0 && <p className={mutedClass}>No findings match.</p>}
      {groups.data?.truncated && (
        <p className={`text-sm ${mutedClass}`}>Only the {formatNumber(items.length)} largest groups are shown. Narrow the filter to see the rest.</p>
      )}
      {items.length > 0 && (
        <ul aria-label={`Findings by ${NOUN[by]}`} className="space-y-2">
          {items.map((group) => {
            const open = group.key === openKey;
            return (
              <li key={group.key} className="rounded-lg border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
                <button
                  type="button"
                  aria-expanded={open}
                  aria-controls={open ? panelId : undefined}
                  onClick={() => onOpen(open ? undefined : group.key)}
                  className="flex min-h-11 w-full flex-wrap items-center gap-x-3 gap-y-1 rounded-lg px-3 py-2 text-left text-sm hover:bg-slate-100 dark:hover:bg-slate-800"
                >
                  <span aria-hidden="true" className="w-3 text-xs">
                    {open ? "▾" : "▸"}
                  </span>
                  <span className={`min-w-0 break-all font-medium ${by === "series" ? "" : "font-mono text-xs"}`}>{groupLabel(by, group)}</span>
                  <span className="tabular-nums">
                    {formatNumber(group.count)} {group.count === 1 ? "finding" : "findings"}
                  </span>
                  <SeverityCounts severities={group.severities} />
                  <span className={`ml-auto text-xs ${mutedClass}`}>latest {formatLocalTime(group.latest)}</span>
                </button>
                {open && (
                  <div id={panelId} className="border-t border-slate-200 p-2 dark:border-slate-800">
                    {children(group)}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
