import { useState } from "react";
import { formatDuration, formatLocalTime, severityClass } from "../format";
import { axisTicks, lanes } from "../timeline";
import type { Finding, NsWindow } from "../types";

const VISIBLE_LANES = 12;
const MIN_MARK_PX = 3;

type Props = {
  findings: Finding[];
  window: NsWindow;
  /** series id → external id, for lane labels on a dataset run. */
  seriesNames?: Record<string, string>;
  /** The run covers several series (a dataset run): lanes split and name the series. */
  multiSeries?: boolean;
  onSelect: (findingId: string) => void;
};

/**
 * Where in the data window each finding lies (spec 020): one lane per check, a mark per finding
 * coloured by severity, on a calendar axis. The findings table stays the complete text view; a
 * mark only adds the position in time and opens its row.
 */
export function FindingsTimeline({ findings, window, seriesNames, multiSeries, onSelect }: Props) {
  const [showAll, setShowAll] = useState(false);
  const span = window.end - window.start;
  if (!(span > 0) || findings.length === 0) return null;
  const all = lanes(findings, seriesNames, multiSeries);
  const shown = showAll ? all : all.slice(0, VISIBLE_LANES);
  const ticks = axisTicks(window.start, window.end);
  const pos = (ns: number) => Math.min(1, Math.max(0, (ns - window.start) / span));

  return (
    <section aria-labelledby="timeline-heading" className="space-y-2">
      <h2 id="timeline-heading" className="text-base font-semibold">
        Where the findings are
      </h2>
      <div className="rounded-lg border border-slate-200 bg-white p-3 text-xs dark:border-slate-800 dark:bg-slate-900">
        <ul className="space-y-1">
          {shown.map((lane) => (
            <li key={lane.key} className="grid grid-cols-[minmax(0,13rem)_1fr] items-center gap-3">
              <span className="min-w-0" title={lane.label}>
                <span className="block truncate font-mono">
                  {lane.check}
                  <span className="text-slate-600 dark:text-slate-400"> ({lane.findings.length})</span>
                </span>
                {lane.series && <span className="block truncate text-slate-600 dark:text-slate-400">{lane.series}</span>}
              </span>
              <div className="relative h-5 rounded bg-slate-100 dark:bg-slate-800">
                {ticks.map((t) => (
                  <span
                    key={t.ns}
                    aria-hidden="true"
                    className="absolute inset-y-0 w-px bg-slate-200 dark:bg-slate-700"
                    style={{ left: `${t.at * 100}%` }}
                  />
                ))}
                {lane.findings.map((f) => {
                  const left = pos(f.window.start);
                  const width = pos(f.window.end) - left;
                  return (
                    <button
                      key={f.id}
                      type="button"
                      onClick={() => onSelect(f.id)}
                      aria-label={`${f.severity} ${f.check_id}: ${f.summary}, ${formatLocalTime(f.window.start)}, ${formatDuration(f.window.end - f.window.start)}`}
                      title={`${f.summary}\n${formatLocalTime(f.window.start)} · ${formatDuration(f.window.end - f.window.start)}`}
                      className={`absolute inset-y-0.5 rounded-sm opacity-90 hover:opacity-100 focus:outline-2 focus:outline-offset-1 focus:outline-sky-600 ${severityClass[f.severity] ?? ""}`}
                      style={{
                        left: `${left * 100}%`,
                        width: `max(${MIN_MARK_PX}px, ${width * 100}%)`,
                        // A mark at the window's end keeps its minimum width inside the track.
                        maxWidth: `calc(100% - ${left * 100}%)`,
                      }}
                    />
                  );
                })}
              </div>
            </li>
          ))}
        </ul>
        <div aria-hidden="true" className="mt-1 grid grid-cols-[minmax(0,13rem)_1fr] gap-3">
          <span />
          <div className="relative h-4 text-slate-600 dark:text-slate-400">
            {ticks.map((t) => (
              <span
                key={t.ns}
                // Centred on its tick, but kept inside the track at both ends.
                className={`absolute whitespace-nowrap ${t.at < 0.04 ? "" : t.at > 0.96 ? "-translate-x-full" : "-translate-x-1/2"}`}
                style={{ left: `${t.at * 100}%` }}
              >
                {t.label}
              </span>
            ))}
          </div>
        </div>
        {all.length > VISIBLE_LANES && (
          <button
            type="button"
            onClick={() => setShowAll((v) => !v)}
            className="mt-2 font-medium text-sky-800 underline underline-offset-2 dark:text-sky-300"
          >
            {showAll ? `Show the first ${VISIBLE_LANES} checks` : `Show all ${all.length} checks`}
          </button>
        )}
      </div>
    </section>
  );
}
