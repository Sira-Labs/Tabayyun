import { useState, type FormEvent } from "react";
import { buttonClass } from "../../components/Brand";
import { LAYOUTS, NORMS, type Layout, type Norm } from "../../compare/url";
import type { Range } from "../../seriesChart";
import { inputClass, selectClass } from "../admin/ui";
import { localInput, nsOfInput } from "../SeriesDetail";

const LAYOUT_LABEL: Record<Layout, string> = { auto: "By unit", overlay: "One plot", lanes: "One lane per series" };
const NORM_LABEL: Record<Norm, string> = { raw: "Raw values", range: "Share of range", z: "z-score", index: "Index (first = 100)" };

type Props = {
  range: Range;
  layout: Layout;
  norm: Norm;
  onRange: (next: Range) => void;
  onReset: () => void;
  onBack: () => void;
  canGoBack: boolean;
  onLayout: (layout: Layout) => void;
  onNorm: (norm: Norm) => void;
  scatter: boolean;
  onScatter: () => void;
};

/** The window (From, To, Reset, Back), how the series are laid out and normalised, and the scatter's switch. */
export function Controls({ range, layout, norm, onRange, onReset, onBack, canGoBack, onLayout, onNorm, scatter, onScatter }: Props) {
  const [fromText, setFromText] = useState(localInput(range.from));
  const [toText, setToText] = useState(localInput(range.to));
  const [error, setError] = useState<string | null>(null);
  // Follow window changes made elsewhere (zoom, keys, back), keyed on the window shown.
  const [shownKey, setShownKey] = useState(`${range.from}-${range.to}`);
  const key = `${range.from}-${range.to}`;
  if (key !== shownKey) {
    setShownKey(key);
    setFromText(localInput(range.from));
    setToText(localInput(range.to));
    setError(null);
  }
  const submit = (e: FormEvent) => {
    e.preventDefault();
    // The inputs show minutes. A field left as shown keeps its exact endpoint, so a zoomed
    // window of 10:00:30 to 11:00:30 does not shift by half a minute on Show.
    const from = fromText === localInput(range.from) ? range.from : nsOfInput(fromText);
    const to = toText === localInput(range.to) ? range.to : nsOfInput(toText);
    if (!Number.isFinite(from) || !Number.isFinite(to)) return setError("Enter both From and To.");
    if (to <= from) return setError("To must be later than From.");
    setError(null);
    onRange({ from, to });
  };
  const chip = "rounded border border-slate-300 px-2 py-1 text-sm hover:bg-slate-100 disabled:opacity-50 dark:border-slate-700 dark:hover:bg-slate-800";
  return (
    <div className="flex flex-wrap items-end gap-x-4 gap-y-2">
      <form onSubmit={submit} aria-label="Window" className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col gap-1 text-sm">
          From
          <input type="datetime-local" className={inputClass} value={fromText} onChange={(e) => setFromText(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          To
          <input type="datetime-local" className={inputClass} value={toText} onChange={(e) => setToText(e.target.value)} />
        </label>
        <button type="submit" className={buttonClass}>
          Show
        </button>
        <button type="button" className={chip} onClick={onReset}>
          Reset
        </button>
        <button type="button" className={chip} onClick={onBack} disabled={!canGoBack}>
          Back
        </button>
        {error && (
          <p role="alert" className="basis-full text-sm text-red-700 dark:text-red-400">
            {error}
          </p>
        )}
      </form>
      <label className="flex flex-col gap-1 text-sm">
        Layout
        <select className={selectClass} value={layout} onChange={(e) => onLayout(e.target.value as Layout)}>
          {LAYOUTS.map((l) => (
            <option key={l} value={l}>
              {LAYOUT_LABEL[l]}
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-col gap-1 text-sm">
        Values
        <select className={selectClass} value={norm} onChange={(e) => onNorm(e.target.value as Norm)}>
          {NORMS.map((n) => (
            <option key={n} value={n}>
              {NORM_LABEL[n]}
            </option>
          ))}
        </select>
      </label>
      <button type="button" className={`${chip} ${scatter ? "bg-slate-200 dark:bg-slate-700" : ""}`} aria-pressed={scatter} onClick={onScatter}>
        Scatter
      </button>
    </div>
  );
}
