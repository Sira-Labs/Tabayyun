import { SeverityBadge } from "../../components/Badges";
import type { CompareSeries } from "../../compare/api";
import { findingsCsv, tableCsv, type TableRow } from "../../compare/table";
import { formatDuration, formatLocalTime, formatNumber } from "../../format";
import type { Finding } from "../../types";
import { linkButtonClass } from "../admin/ui";

// Rows drawn at most; the CSV always holds every bin.
const MAX_ROWS = 500;

/** Offer `text` as a file download. */
function download(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/csv;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

type Props = { series: CompareSeries[]; rows: TableRow[]; findings: Finding[] };

const cell = (v: number | undefined) => (v === undefined ? "—" : formatNumber(v));

/** The chart as tables: each series' minimum and maximum per bin, then the findings in the
 * window; both can be saved as CSV. */
export function CompareTable({ series, rows, findings }: Props) {
  const nameOf = (id: string) => series.find((s) => s.id === id)?.name ?? id;
  return (
    <div className="space-y-4 text-sm">
      <div className="flex flex-wrap gap-2">
        <button type="button" className={linkButtonClass} onClick={() => download("compare.csv", tableCsv(series, rows))}>
          Export the table as CSV
        </button>
        <button type="button" className={linkButtonClass} disabled={findings.length === 0} onClick={() => download("compare-findings.csv", findingsCsv(findings, nameOf))}>
          Export the findings as CSV
        </button>
      </div>
      {rows.length > MAX_ROWS && (
        <p className="text-xs text-slate-600 dark:text-slate-400">
          Showing the first {MAX_ROWS} of {formatNumber(rows.length)} bins; the CSV holds all of them.
        </p>
      )}
      <div className="max-h-96 overflow-auto rounded border border-slate-200 dark:border-slate-800">
        <table className="w-full text-left text-xs">
          <caption className="sr-only">Minimum and maximum of each series per chart bin</caption>
          <thead className="sticky top-0 bg-slate-100 dark:bg-slate-800">
            <tr>
              <th scope="col" className="p-2">Bin start</th>
              {series.map((s) => (
                <th key={s.id} scope="col" colSpan={2} className="p-2">
                  {s.name}
                  {s.unit ? ` (${s.unit})` : ""}, min and max
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.slice(0, MAX_ROWS).map((r) => (
              <tr key={r.bin} className="border-t border-slate-200 dark:border-slate-800">
                <th scope="row" className="whitespace-nowrap p-2 font-normal">{formatLocalTime(r.from)}</th>
                {r.cells.flatMap((c, i) => [
                  <td key={`${i}-min`} className="p-2 tabular-nums">{cell(c?.min)}</td>,
                  <td key={`${i}-max`} className="p-2 tabular-nums">{cell(c?.max)}</td>,
                ])}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <table className="w-full text-left text-xs">
        <caption className="pb-1 text-left text-sm font-medium">Findings in this window ({findings.length})</caption>
        <thead className="bg-slate-100 dark:bg-slate-800">
          <tr>
            <th scope="col" className="p-2">Start</th>
            <th scope="col" className="p-2">Duration</th>
            <th scope="col" className="p-2">Series</th>
            <th scope="col" className="p-2">Check</th>
            <th scope="col" className="p-2">Severity</th>
            <th scope="col" className="p-2">Summary</th>
          </tr>
        </thead>
        <tbody>
          {findings.map((f) => (
            <tr key={f.id} className="border-t border-slate-200 dark:border-slate-800">
              <td className="whitespace-nowrap p-2">{formatLocalTime(f.window.start)}</td>
              <td className="p-2">{formatDuration(f.window.end - f.window.start)}</td>
              <td className="p-2">{nameOf(f.series_id)}</td>
              <td className="p-2 font-mono">{f.check_id}</td>
              <td className="p-2"><SeverityBadge severity={f.severity} /></td>
              <td className="p-2">{f.summary}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
