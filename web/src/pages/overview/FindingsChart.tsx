import { Link } from "@tanstack/react-router";
import { SeverityBadge } from "../../components/Badges";
import { formatNumber, severityClass } from "../../format";
import { SEVERITIES } from "../../findingsInbox";
import { barSegments, dayTotal, dayWords, findingsWords, severityFindingsSearch, type Overview } from "../../overview";
import type { Severity } from "../../types";
import { mutedClass } from "../suites/parts";

const SLOT = 10;
const BAR = 7;
const HEIGHT = 100;

// The fills of the severity badges (`severityClass` in format.ts), as SVG fills.
const FILL: Record<Severity, string> = {
  critical: "fill-red-700",
  high: "fill-orange-700",
  medium: "fill-amber-300",
  low: "fill-slate-300 dark:fill-slate-600",
};

/** One stacked bar per day by severity; each day names its counts on hover. */
function Bars({ days }: { days: Overview["findings"]["days"] }) {
  const max = Math.max(0, ...days.map(dayTotal));
  const width = Math.max(1, days.length) * SLOT;
  return (
    <svg
      role="img"
      aria-label={findingsWords(days)}
      viewBox={`0 0 ${width} ${HEIGHT}`}
      preserveAspectRatio="none"
      className="h-32 w-full"
    >
      <line x1={0} x2={width} y1={HEIGHT} y2={HEIGHT} className="stroke-slate-300 dark:stroke-slate-700" vectorEffect="non-scaling-stroke" />
      {days.map((day, i) => (
        <g key={day.day}>
          <title>{dayWords(day)}</title>
          {/* The whole slot answers the hover, not only the bar. */}
          <rect x={i * SLOT} y={0} width={SLOT} height={HEIGHT} className="fill-transparent" />
          {barSegments(day, max, HEIGHT).map((seg) => (
            <rect
              key={seg.severity}
              x={i * SLOT + (SLOT - BAR) / 2}
              y={seg.y}
              width={BAR}
              height={seg.height}
              // A thin edge in the panel's colour keeps neighbouring segments apart.
              className={`${FILL[seg.severity]} stroke-white dark:stroke-slate-900`}
              strokeWidth={1}
              vectorEffect="non-scaling-stroke"
            />
          ))}
        </g>
      ))}
    </svg>
  );
}

/** The chart's numbers as a table, inside a disclosure. */
function DaysTable({ days }: { days: Overview["findings"]["days"] }) {
  return (
    <details className="text-sm">
      <summary className="cursor-pointer font-medium text-sky-800 underline underline-offset-2 dark:text-sky-300">Show as a table</summary>
      <div className="mt-2 max-h-80 overflow-auto">
        <table aria-label="New findings per day by severity" className="w-full text-left text-sm tabular-nums">
          <thead className="text-xs uppercase tracking-wide text-slate-600 dark:text-slate-400">
            <tr>
              <th scope="col" className="px-2 py-1">Day</th>
              {SEVERITIES.map((s) => (
                <th key={s} scope="col" className="px-2 py-1 text-right">
                  {s}
                </th>
              ))}
              <th scope="col" className="px-2 py-1 text-right">Total</th>
            </tr>
          </thead>
          <tbody>
            {days.map((day) => (
              <tr key={day.day} className="border-t border-slate-100 dark:border-slate-800">
                <th scope="row" className="px-2 py-1 font-normal">
                  {day.day}
                </th>
                {SEVERITIES.map((s) => (
                  <td key={s} className="px-2 py-1 text-right">
                    {formatNumber(day[s])}
                  </td>
                ))}
                <td className="px-2 py-1 text-right">{formatNumber(dayTotal(day))}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

/** The severities' swatches; the chart's colours are never its only label. */
function Legend() {
  return (
    <ul aria-label="Severities" className="flex flex-wrap gap-3 text-xs">
      {SEVERITIES.map((s) => (
        <li key={s} className="flex items-center gap-1">
          <span aria-hidden="true" className={`inline-block size-3 rounded-sm ${severityClass[s] ?? ""}`} />
          {s}
        </li>
      ))}
    </ul>
  );
}

/** The unresolved findings by severity, each linking to the inbox with that filter. */
function Unresolved({ open }: { open: Overview["findings"]["open"] }) {
  return (
    <div className="flex flex-wrap items-center gap-3 text-sm">
      <span className="font-medium">Unresolved</span>
      <ul aria-label="Unresolved findings by severity" className="flex flex-wrap gap-3">
        {SEVERITIES.map((s) => (
          <li key={s}>
            <Link
              to="/findings"
              search={severityFindingsSearch(s)}
              aria-label={`${formatNumber(open[s] ?? 0)} unresolved ${s}`}
              className="inline-flex items-center gap-1 underline-offset-2 hover:underline"
            >
              <SeverityBadge severity={s} />
              <span className="tabular-nums">{formatNumber(open[s] ?? 0)}</span>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** "New findings per day": a stacked bar per day by severity with its table, and the
 * unresolved counts. */
export function FindingsPerDay({ findings }: { findings: Overview["findings"] }) {
  const first = findings.days[0];
  const last = findings.days.at(-1);
  const max = Math.max(0, ...findings.days.map(dayTotal));
  return (
    <section aria-labelledby="overview-findings-heading" className="space-y-2">
      <h2 id="overview-findings-heading" className="text-base font-semibold">
        New findings per day
      </h2>
      <div className="space-y-3 rounded-lg border border-slate-200 bg-white p-3 dark:border-slate-800 dark:bg-slate-900">
        <Unresolved open={findings.open} />
        <Legend />
        <div>
          <p aria-hidden="true" className={`text-xs ${mutedClass}`}>
            {max > 0 ? `up to ${formatNumber(max)} a day` : "no new findings"}
          </p>
          <Bars days={findings.days} />
          {first && last && (
            <p aria-hidden="true" className={`flex justify-between text-xs ${mutedClass}`}>
              <span>{first.day}</span>
              <span>{last.day}</span>
            </p>
          )}
        </div>
        <DaysTable days={findings.days} />
      </div>
    </section>
  );
}
