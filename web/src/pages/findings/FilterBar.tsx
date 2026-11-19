import { useQuery } from "@tanstack/react-query";
import {
  DIMENSIONS,
  SEVERITIES,
  STATUSES,
  hasFilters,
  inboxApi,
  inboxKeys,
  severitiesOf,
  severityParam,
  statusParam,
  statusesOf,
  type InboxSearch,
} from "../../findingsInbox";
import { linkButtonClass, selectClass } from "../admin/ui";
import { DebouncedText, fieldClass } from "./parts";

const STATUS_LABEL: Record<(typeof STATUSES)[number], string> = {
  open: "open",
  acked: "acknowledged",
  muted: "muted",
  resolved: "resolved",
};

/** `list` with `item` added, or removed when it was in. */
function flipped<T>(list: readonly T[], item: T): T[] {
  return list.includes(item) ? list.filter((x) => x !== item) : [...list, item];
}

/** A group of checkboxes with a legend: one filter that takes several values. */
function Choices<T extends string>({
  legend,
  options,
  chosen,
  label,
  onToggle,
}: {
  legend: string;
  options: readonly T[];
  chosen: readonly T[];
  label: (option: T) => string;
  onToggle: (option: T) => void;
}) {
  return (
    <fieldset className="text-sm">
      <legend className="mb-1">{legend}</legend>
      <div className="flex min-h-9 flex-wrap items-center gap-x-3 gap-y-1">
        {options.map((option) => (
          <label key={option} className="inline-flex items-center gap-1">
            <input type="checkbox" checked={chosen.includes(option)} onChange={() => onToggle(option)} />
            {label(option)}
          </label>
        ))}
      </div>
    </fieldset>
  );
}

/** The inbox's filters (spec 029). The URL holds their state: `search` is what it says now,
 * `onChange` writes a change to it; text typed into a box replaces the current history entry. */
export function FilterBar({
  search,
  onChange,
}: {
  search: InboxSearch;
  onChange: (change: Partial<InboxSearch>, options?: { replace?: boolean }) => void;
}) {
  const assignees = useQuery({ queryKey: inboxKeys.assignees, queryFn: inboxApi.assignees, staleTime: 60_000 });
  const people = assignees.data?.items ?? [];
  const statuses = statusesOf(search);
  const severities = severitiesOf(search);
  const named = search.assignee !== undefined && search.assignee !== "me" && search.assignee !== "none";

  return (
    <form role="search" aria-label="Filter findings" className="flex flex-wrap items-end gap-x-4 gap-y-3" onSubmit={(e) => e.preventDefault()}>
      <Choices
        legend="Status"
        options={STATUSES}
        chosen={statuses}
        label={(s) => STATUS_LABEL[s]}
        onToggle={(s) => onChange({ status: statusParam(flipped(statuses, s)) })}
      />
      <Choices
        legend="Severity"
        options={SEVERITIES}
        chosen={severities}
        label={(s) => s}
        onToggle={(s) => onChange({ severity: severityParam(flipped(severities, s)) })}
      />
      <label className={fieldClass}>
        Dimension
        <select className={selectClass} value={search.dimension ?? ""} onChange={(e) => onChange({ dimension: e.target.value || undefined })}>
          <option value="">Any dimension</option>
          {DIMENSIONS.map((d) => (
            <option key={d} value={d}>
              {d}
            </option>
          ))}
        </select>
      </label>
      <label className={fieldClass}>
        Assignee
        <select className={selectClass} value={search.assignee ?? ""} onChange={(e) => onChange({ assignee: e.target.value || undefined })}>
          <option value="">Anyone</option>
          <option value="me">Me</option>
          <option value="none">Unassigned</option>
          {people.map((p) => (
            <option key={p.id} value={p.id}>
              {p.display_name}
            </option>
          ))}
          {named && !people.some((p) => p.id === search.assignee) && <option value={search.assignee}>{search.assignee}</option>}
        </select>
      </label>
      <label className={fieldClass}>
        Feedback
        <select
          className={selectClass}
          value={search.feedback ?? ""}
          onChange={(e) => onChange({ feedback: (e.target.value || undefined) as InboxSearch["feedback"] })}
        >
          <option value="">Any feedback</option>
          <option value="false_positive">False positive</option>
          <option value="none">No feedback</option>
        </select>
      </label>
      <DebouncedText
        label="Check"
        value={search.check_id}
        placeholder="Exact check id"
        maxLength={100}
        mono
        onCommit={(check_id) => onChange({ check_id }, { replace: true })}
      />
      <DebouncedText
        label="Search"
        value={search.q}
        placeholder="Summary or series name"
        maxLength={200}
        onCommit={(q) => onChange({ q }, { replace: true })}
      />
      <button type="button" className={linkButtonClass} disabled={!hasFilters(search)} onClick={() => onChange(CLEARED)}>
        Clear filters
      </button>
    </form>
  );
}

const CLEARED: Partial<InboxSearch> = {
  status: undefined,
  severity: undefined,
  dimension: undefined,
  check_id: undefined,
  assignee: undefined,
  feedback: undefined,
  q: undefined,
};
