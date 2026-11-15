import type { CompareSeries } from "../../compare/api";
import { seriesColour, type Theme } from "../../compare/palette";
import type { Reading } from "../../compare/table";
import { formatLocalTime, formatNumber } from "../../format";

/** A raw value with its unit, as the readout and the announcement show it. */
function valueText(value: number, unit: string | null): string {
  return unit ? `${formatNumber(value)} ${unit}` : formatNumber(value);
}

/** The readout as one sentence, for the live region. */
export function readoutText(series: CompareSeries[], readings: Reading[], at: number): string {
  const parts = series.map((s, i) => `${s.name} ${readings[i] ? valueText(readings[i].value, s.unit) : "no value"}`);
  return `${formatLocalTime(at)}: ${parts.join("; ")}`;
}

type Props = { series: CompareSeries[]; readings: Reading[] | null; at: number | null; theme: Theme };

/** The raw value of every series at the cursor, with units, whatever the normalisation. */
export function Readout({ series, readings, at, theme }: Props) {
  if (!readings || at === null) {
    return <p className="mt-2 text-xs text-slate-600 dark:text-slate-400">Move over the chart, or focus it and press the arrow keys, to read values.</p>;
  }
  return (
    <div className="mt-2 text-xs" data-testid="compare-readout">
      <p className="text-slate-600 dark:text-slate-400">Raw values at {formatLocalTime(at)}</p>
      <dl className="flex flex-wrap gap-x-4 gap-y-1">
        {series.map((s, i) => (
          <div key={s.id} className="flex items-center gap-1">
            <span aria-hidden className="inline-block h-2 w-2 rounded-full" style={{ backgroundColor: seriesColour(i, theme) }} />
            <dt>{s.name}</dt>
            <dd className="font-medium tabular-nums">{readings[i] ? valueText(readings[i].value, s.unit) : "—"}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
