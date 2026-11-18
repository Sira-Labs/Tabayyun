// Building blocks of the findings inbox (spec 029).
import { useEffect, useRef, useState } from "react";
import { SeverityBadge } from "../../components/Badges";
import { formatLocalTime, formatNumber } from "../../format";
import { SEVERITIES, type FindingGroup, type InboxFinding } from "../../findingsInbox";
import { inputClass } from "../admin/ui";

export const mutedClass = "text-slate-600 dark:text-slate-400";
export const fieldClass = "flex flex-col gap-1 text-sm";
/** How long a text filter waits for the next key before it goes into the URL; tests shorten it. */
export const debounce = { ms: 300 };

const STATUS_CLASS: Record<string, string> = {
  open: "bg-sky-100 text-sky-900 dark:bg-sky-950 dark:text-sky-200",
  acked: "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200",
  muted: "bg-slate-200 text-slate-800 dark:bg-slate-700 dark:text-slate-100",
  resolved: "bg-emerald-100 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-200",
};

/** A finding's status, the end of its mute when it has one, and its false-positive mark. */
export function FindingStatus({ finding }: { finding: InboxFinding }) {
  const until = finding.status === "muted" && finding.muted_until ? formatLocalTime(finding.muted_until) : null;
  return (
    <span className="flex flex-wrap items-center gap-x-1 gap-y-0.5 text-xs">
      <span className={`rounded px-2 py-0.5 font-medium ${STATUS_CLASS[finding.status] ?? ""}`}>{finding.status}</span>
      {finding.feedback === "false_positive" && (
        <span title="Marked as a false positive" className="rounded border border-slate-400 px-1 font-medium dark:border-slate-500">
          <span aria-hidden="true">FP</span>
          <span className="sr-only">false positive</span>
        </span>
      )}
      {until && (
        <span title={until} className={mutedClass}>
          until {until.slice(0, 16)}
        </span>
      )}
    </span>
  );
}

/** A group's findings by severity, most severe first, as small badges. */
export function SeverityCounts({ severities }: { severities: FindingGroup["severities"] }) {
  return (
    <span className="flex flex-wrap gap-1">
      {SEVERITIES.filter((s) => (severities[s] ?? 0) > 0).map((s) => (
        <span key={s} className="inline-flex items-center gap-1 text-xs tabular-nums">
          <SeverityBadge severity={s} />
          <span aria-hidden="true">×</span>
          {formatNumber(severities[s] ?? 0)}
        </span>
      ))}
    </span>
  );
}

/** A text filter that follows `value` (the URL) and reports what was typed once the typing
 * pauses: trimmed, or undefined when empty. */
export function DebouncedText({
  label,
  value,
  onCommit,
  placeholder,
  maxLength,
  mono,
}: {
  label: string;
  value: string | undefined;
  onCommit: (value: string | undefined) => void;
  placeholder?: string;
  maxLength: number;
  mono?: boolean;
}) {
  const [text, setText] = useState(value ?? "");
  // What this box last put into the URL: a `value` that differs came from elsewhere (Back,
  // Forward, a saved filter, "Clear filters") and replaces the text; its own echo does not,
  // so it cannot undo keys typed since.
  const [sent, setSent] = useState(value);
  const [seen, setSeen] = useState(value);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  // Counts the values that came from elsewhere, so the effect below can drop a commit that
  // is still waiting: it would put the text typed before over the value that replaced it.
  const [replaced, setReplaced] = useState(0);
  if (value !== seen) {
    setSeen(value);
    if (value !== sent) {
      setSent(value);
      setText(value ?? "");
      setReplaced(replaced + 1);
    }
  }
  useEffect(() => () => clearTimeout(timer.current), [replaced]);

  const change = (next: string) => {
    setText(next);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      const committed = next.trim() || undefined;
      setSent(committed);
      onCommit(committed);
    }, debounce.ms);
  };

  return (
    <label className={fieldClass}>
      {label}
      <input
        className={`${inputClass} ${mono ? "font-mono" : ""}`}
        value={text}
        placeholder={placeholder}
        maxLength={maxLength}
        onChange={(e) => change(e.target.value)}
      />
    </label>
  );
}
