import type { CompareSeries } from "../../compare/api";
import type { Emphasis } from "../../compare/findings";
import type { Normalised } from "../../compare/normalise";
import { seriesColour, seriesDash, type Theme } from "../../compare/palette";
import { formatNumber } from "../../format";

type Props = {
  series: CompareSeries[];
  norms: Record<string, Normalised>;
  emphasis: Record<string, Emphasis>;
  theme: Theme;
  hidden: ReadonlySet<string>;
  onToggle: (id: string) => void;
};

/** One button per series: its line sample, name and unit; pressing it shows or hides the line.
 * A series without a line says why instead. */
export function Legend({ series, norms, emphasis, theme, hidden, onToggle }: Props) {
  return (
    <ul aria-label="Series" className="mt-2 flex flex-wrap gap-2 text-sm">
      {series.map((s, i) => {
        const norm = norms[s.id];
        const reason = s.ts.length === 0 ? "no data" : norm && !norm.ok ? norm.reason : null;
        const shown = reason === null && !hidden.has(s.id);
        return (
          <li key={s.id}>
            <button
              type="button"
              aria-pressed={shown}
              disabled={reason !== null}
              onClick={() => onToggle(s.id)}
              className={`flex min-h-9 items-center gap-2 rounded border border-slate-300 px-2 py-1 text-left hover:bg-slate-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-600 dark:border-slate-700 dark:hover:bg-slate-800 ${
                shown ? "" : "opacity-60"
              }`}
            >
              <span
                aria-hidden
                className="inline-block w-6 border-t-2"
                style={{ borderColor: seriesColour(i, theme), borderTopStyle: seriesDash(i) ? "dashed" : "solid" }}
              />
              <span className={shown ? "font-medium" : "font-medium line-through"}>{s.name}</span>
              <span className="text-xs text-slate-600 dark:text-slate-400">{s.unit ?? "no unit"}</span>
              {emphasis[s.id] === "suspect" && <span className="rounded bg-amber-300 px-1 text-xs font-semibold text-slate-900">suspect</span>}
              {reason !== null ? (
                <span className="text-xs text-slate-700 dark:text-slate-300">{reason}</span>
              ) : (
                <span className="text-xs text-slate-600 dark:text-slate-400">{formatNumber(s.n_raw)} samples</span>
              )}
            </button>
          </li>
        );
      })}
    </ul>
  );
}
