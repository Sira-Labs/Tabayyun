import { Link } from "@tanstack/react-router";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useEffect, useRef, type CSSProperties } from "react";
import { SeverityBadge } from "../../components/Badges";
import { formatLocalTime } from "../../format";
import { selectionState, type InboxFinding } from "../../findingsInbox";
import { FindingStatus } from "./parts";

/** Every row is this high, so the list's height is known without measuring a row. */
export const ROW_HEIGHT = 48;
const HEADER_HEIGHT = 36;
/** The next page is asked for when the rows drawn come this close to the last one loaded. */
const NEAR_END = 40;

const COLUMNS = "11.5rem minmax(7rem,1fr) 9rem 5.5rem 9.5rem 7rem minmax(10rem,2fr)";
const cellClass = "flex min-w-0 items-center px-2";

/** Row selection of the table: what is selected and how a row, or all loaded rows, change it.
 * While `locked` the checkboxes show the selection and cannot change it. */
export type TableSelection = {
  selected: ReadonlySet<string>;
  locked: boolean;
  onToggle: (id: string) => void;
  onToggleAll: () => void;
};

/** The findings of a filter as a virtualised table (spec 029): only the rows near the visible
 * part are in the document, whatever the number loaded, and the next page is asked for when
 * the rows drawn near the end. `aria-rowcount` is the header plus every loaded row, or -1
 * while further pages exist; each row carries its `aria-rowindex`. Without `selection`
 * (viewers) there is no checkbox column. */
export function FindingsTable({
  items,
  label,
  hasMore,
  autoLoad,
  onLoadMore,
  selection,
}: {
  items: InboxFinding[];
  label: string;
  hasMore: boolean;
  /** Whether nearing the end may ask for the next page now: not while one loads or after one failed. */
  autoLoad: boolean;
  onLoadMore: () => void;
  selection: TableSelection | null;
}) {
  // The virtualiser hands out functions that change without changing identity, so the React
  // compiler leaves this component unmemoised (the lint notice on the next call says so).
  const scroller = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 8,
    scrollMargin: HEADER_HEIGHT,
  });
  const rows = virtualizer.getVirtualItems();
  const lastDrawn = rows[rows.length - 1]?.index ?? -1;
  const nearEnd = lastDrawn >= 0 && lastDrawn >= items.length - NEAR_END;
  useEffect(() => {
    if (nearEnd && hasMore && autoLoad) onLoadMore();
  }, [nearEnd, hasMore, autoLoad, onLoadMore]);

  const grid: CSSProperties = { display: "grid", gridTemplateColumns: selection ? `2.75rem ${COLUMNS}` : COLUMNS };
  const all = selection ? selectionState(items.map((f) => f.id), selection.selected) : "none";

  return (
    <div ref={scroller} className="max-h-[32rem] overflow-auto rounded-lg border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
      <div role="table" aria-label={label} aria-rowcount={hasMore ? -1 : items.length + 1} className="min-w-[61rem] text-sm">
        <div role="rowgroup" className="sticky top-0 z-10 bg-slate-100 text-xs uppercase tracking-wide text-slate-700 dark:bg-slate-800 dark:text-slate-300">
          <div role="row" aria-rowindex={1} style={{ ...grid, height: HEADER_HEIGHT }}>
            {selection && (
              <div role="columnheader" className={`${cellClass} justify-center`}>
                <input
                  type="checkbox"
                  aria-label="Select all loaded findings"
                  checked={all === "all"}
                  disabled={selection.locked}
                  ref={(box) => {
                    if (box) box.indeterminate = all === "some";
                  }}
                  onChange={selection.onToggleAll}
                />
              </div>
            )}
            {["Window start", "Series", "Check", "Severity", "Status", "Assignee", "Summary"].map((name) => (
              <div key={name} role="columnheader" className={cellClass}>
                {name}
              </div>
            ))}
          </div>
        </div>
        <div role="rowgroup" className="relative" style={{ height: items.length * ROW_HEIGHT }}>
          {rows.map((row) => {
            const finding = items[row.index];
            if (!finding) return null;
            return (
              <Row
                key={finding.id}
                finding={finding}
                index={row.index}
                top={row.start - HEADER_HEIGHT}
                grid={grid}
                checked={selection ? selection.selected.has(finding.id) : null}
                locked={selection?.locked ?? false}
                onToggle={selection?.onToggle}
              />
            );
          })}
        </div>
      </div>
    </div>
  );
}

/** One finding, placed where the virtualiser says. `checked` is null without a checkbox. */
function Row({
  finding,
  index,
  top,
  grid,
  checked,
  locked,
  onToggle,
}: {
  finding: InboxFinding;
  index: number;
  top: number;
  grid: CSSProperties;
  checked: boolean | null;
  locked: boolean;
  onToggle: ((id: string) => void) | undefined;
}) {
  const start = formatLocalTime(finding.window.start);
  return (
    <div
      role="row"
      aria-rowindex={index + 2}
      aria-selected={checked ?? undefined}
      className={`absolute left-0 top-0 w-full border-t border-slate-200 dark:border-slate-800 ${checked ? "bg-sky-50 dark:bg-sky-950" : ""}`}
      style={{ ...grid, height: ROW_HEIGHT, transform: `translateY(${top}px)` }}
    >
      {checked !== null && (
        <div role="cell" className={`${cellClass} justify-center`}>
          <input
            type="checkbox"
            aria-label={`Select ${finding.series_name}, ${finding.check_id}, ${start}`}
            checked={checked}
            disabled={locked}
            onChange={() => onToggle?.(finding.id)}
          />
        </div>
      )}
      <div role="cell" className={`${cellClass} text-xs tabular-nums`}>
        {start}
      </div>
      <div role="cell" className={cellClass}>
        <Link
          to="/series/$seriesId"
          params={{ seriesId: finding.series_id }}
          title={finding.asset_path ? `${finding.series_name} (${finding.asset_path})` : finding.series_name}
          className="truncate font-medium text-sky-800 underline underline-offset-2 dark:text-sky-300"
        >
          {finding.series_name}
        </Link>
      </div>
      <div role="cell" className={cellClass}>
        <span title={finding.check_id} className="truncate font-mono text-xs">
          {finding.check_id}
        </span>
      </div>
      <div role="cell" className={cellClass}>
        <SeverityBadge severity={finding.severity} />
      </div>
      <div role="cell" className={cellClass}>
        <FindingStatus finding={finding} />
      </div>
      <div role="cell" className={cellClass}>
        <span className="truncate">{finding.assignee?.display_name ?? "—"}</span>
      </div>
      <div role="cell" className={cellClass}>
        <span title={finding.summary} className="truncate">
          {finding.summary}
        </span>
      </div>
    </div>
  );
}
