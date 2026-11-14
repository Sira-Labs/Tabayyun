import { SeverityBadge } from "../../components/Badges";
import { isCrossSeries } from "../../compare/findings";
import { formatDuration, formatLocalTime } from "../../format";
import type { Finding } from "../../types";
import { panelClass } from "../admin/ui";

type Props = {
  findings: Finding[];
  selected: string | undefined;
  loading: boolean;
  nameOf: (seriesId: string) => string;
  onPick: (finding: Finding) => void;
};

/** The findings of every series in the window; picking one selects it and centres the window on it. */
export function CompareFindings({ findings, selected, loading, nameOf, onPick }: Props) {
  return (
    <section aria-labelledby="compare-findings" className={panelClass}>
      <h2 id="compare-findings" className="mb-2 text-base font-semibold">
        Findings in this window
      </h2>
      {loading ? (
        <p className="text-sm text-slate-600 dark:text-slate-400">Loading…</p>
      ) : findings.length === 0 ? (
        <p className="text-sm text-slate-600 dark:text-slate-400">No findings in this window.</p>
      ) : (
        <ul className="divide-y divide-slate-200 dark:divide-slate-800">
          {findings.map((f) => (
            <li key={f.id}>
              <button
                type="button"
                aria-current={f.id === selected ? "true" : undefined}
                onClick={() => onPick(f)}
                className={`flex w-full flex-col gap-1 px-2 py-2 text-left text-sm hover:bg-slate-50 dark:hover:bg-slate-800 ${
                  f.id === selected ? "bg-sky-50 dark:bg-sky-950" : ""
                }`}
              >
                <span className="flex flex-wrap items-center gap-2">
                  <SeverityBadge severity={f.severity} />
                  <span className="font-mono text-xs">{f.check_id}</span>
                  <span className="text-xs">{isCrossSeries(f) ? "across series" : nameOf(f.series_id)}</span>
                  <span className="text-xs text-slate-600 dark:text-slate-400">
                    {formatLocalTime(f.window.start)} · {formatDuration(f.window.end - f.window.start)}
                    {f.status !== "open" ? ` · ${f.status}` : ""}
                  </span>
                </span>
                <span>{f.summary}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
