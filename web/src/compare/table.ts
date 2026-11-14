// The table view and the cursor readout of the compare view (spec 018, behaviours 4 and 9):
// the chart's bins, each series' minimum and maximum per bin, and CSV export. Pure.
import type { Finding } from "../types";
import type { CompareChartResponse, CompareSeries } from "./api";

/** The window drawn, cut into `width` equal bins; times in ns. */
export type Bins = { from: number; to: number; width: number };

/** The bins of a chart answer, or null when it reports no window (nothing cached). */
export function binsOf(chart: CompareChartResponse): Bins | null {
  if (chart.bins.from === null || chart.bins.to === null) return null;
  const from = Number(chart.bins.from);
  const to = Number(chart.bins.to);
  return to > from && chart.bins.width_px > 0 ? { from, to, width: chart.bins.width_px } : null;
}

/** The bin a time falls in, clamped to the window. */
export function binIndex(ts: number, bins: Bins): number {
  const i = Math.floor(((ts - bins.from) / (bins.to - bins.from)) * bins.width);
  return Math.min(bins.width - 1, Math.max(0, i));
}

/** The start and end of a bin. */
export function binWindow(bin: number, bins: Bins): { from: number; to: number } {
  const step = (bins.to - bins.from) / bins.width;
  return { from: Math.round(bins.from + bin * step), to: Math.round(bins.from + (bin + 1) * step) };
}

export type Cell = { min: number; max: number } | null;
export type TableRow = { bin: number; from: number; to: number; cells: Cell[] };

/** One row per bin that holds a point of any series, in time order; `cells` follow `series`. */
export function tableRows(series: CompareSeries[], bins: Bins): TableRow[] {
  const rows = new Map<number, TableRow>();
  series.forEach((s, column) => {
    s.ts.forEach((t, i) => {
      const value = s.values[i];
      if (value === null || value === undefined) return;
      const bin = binIndex(Number(t), bins);
      let row = rows.get(bin);
      if (!row) {
        row = { bin, ...binWindow(bin, bins), cells: series.map(() => null) };
        rows.set(bin, row);
      }
      const cell = row.cells[column];
      row.cells[column] = cell ? { min: Math.min(cell.min, value), max: Math.max(cell.max, value) } : { min: value, max: value };
    });
  });
  return [...rows.values()].sort((a, b) => a.bin - b.bin);
}

export type Reading = { id: string; ts: number; value: number } | null;

/** Per series, the point inside `bin` nearest to `cursor` (ns); null where the bin is empty. */
export function readoutAt(series: CompareSeries[], bins: Bins, bin: number, cursor: number): Reading[] {
  return series.map((s) => {
    let best: Reading = null;
    s.ts.forEach((t, i) => {
      const value = s.values[i];
      const ts = Number(t);
      if (value === null || value === undefined || binIndex(ts, bins) !== bin) return;
      if (!best || Math.abs(ts - cursor) < Math.abs(best.ts - cursor)) best = { id: s.id, ts, value };
    });
    return best;
  });
}

/** The row `step` rows after the one at or before `bin`; from no cursor, the first or last row. */
export function stepBin(rows: TableRow[], bin: number | null, step: number): number | null {
  if (rows.length === 0) return null;
  if (bin === null) return (step > 0 ? rows[0]! : rows.at(-1)!).bin;
  let at = rows.findIndex((r) => r.bin >= bin);
  if (at === -1) at = rows.length;
  // Between two rows: stepping forward lands on the next row, back on the previous one.
  if (rows[at]?.bin !== bin && step > 0) at -= 1;
  return rows[Math.min(rows.length - 1, Math.max(0, at + step))]!.bin;
}

const isoOfNs = (ns: number) => new Date(ns / 1e6).toISOString();

/** A CSV field: quoted when needed; text a spreadsheet would run as a formula is defused. */
function field(value: string | number | null): string {
  if (value === null) return "";
  if (typeof value === "number") return String(value);
  const text = /^[=+\-@\t\r]/.test(value) ? `'${value}` : value;
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

const line = (fields: (string | number | null)[]) => fields.map(field).join(",");

/** The bin table as CSV: bin start and end (UTC), then each series' min and max. */
export function tableCsv(series: CompareSeries[], rows: TableRow[]): string {
  const head = ["bin_start_utc", "bin_end_utc", ...series.flatMap((s) => [`${s.name} min${s.unit ? ` (${s.unit})` : ""}`, `${s.name} max${s.unit ? ` (${s.unit})` : ""}`])];
  const body = rows.map((r) => line([isoOfNs(r.from), isoOfNs(r.to), ...r.cells.flatMap((c) => [c?.min ?? null, c?.max ?? null])]));
  return [line(head), ...body].join("\r\n") + "\r\n";
}

/** The findings in the window as CSV. */
export function findingsCsv(findings: Finding[], nameOf: (seriesId: string) => string): string {
  const head = ["start_utc", "end_utc", "series", "check_id", "severity", "status", "summary"];
  const body = findings.map((f) => line([isoOfNs(f.window.start), isoOfNs(f.window.end), nameOf(f.series_id), f.check_id, f.severity, f.status, f.summary]));
  return [line(head), ...body].join("\r\n") + "\r\n";
}
