import { useQueries, useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { ApiError, api } from "../../api";
import { buttonClass } from "../../components/Brand";
import { seriesApi } from "../../sources";
import { ErrorLine, inputClass, linkButtonClass } from "../admin/ui";

type Picked = { id: string; name?: string };

type Props = {
  /** Ids chosen before the picker opens; their names are looked up. */
  initial: string[];
  /** Ids the catalogue list leaves out (the series page's own series). */
  exclude?: string[];
  /** How many series may be chosen. */
  min: number;
  max: number;
  submitLabel: string;
  onSubmit: (ids: string[]) => void;
};

/** Choose series from the catalogue: search, tick, and remove what is chosen. */
export function SeriesPicker({ initial, exclude = [], min, max, submitLabel, onSubmit }: Props) {
  const [picked, setPicked] = useState<Picked[]>(() => initial.map((id) => ({ id })));
  const [text, setText] = useState("");
  const [q, setQ] = useState("");
  const catalogue = useQuery({ queryKey: ["series", "list", { q }], queryFn: () => seriesApi.list(q ? { q } : {}) });
  // Names of the ids that came with the URL; a 404 marks an id as unknown.
  const looked = useQueries({
    queries: initial.map((id) => ({ queryKey: ["series", id], queryFn: () => api.getSeries(id), retry: false })),
  });
  const nameOf = (p: Picked): string => {
    if (p.name) return p.name;
    const at = initial.indexOf(p.id);
    const found = at === -1 ? undefined : looked[at];
    if (found?.data) return found.data.name;
    return found?.error instanceof ApiError && found.error.status === 404 ? `unknown series ${p.id}` : p.id;
  };
  const chosen = (id: string) => picked.some((p) => p.id === id);
  const toggle = (id: string, name: string) =>
    setPicked((prev) => (prev.some((p) => p.id === id) ? prev.filter((p) => p.id !== id) : [...prev, { id, name }]));
  const search = (e: FormEvent) => {
    e.preventDefault();
    setQ(text.trim());
  };
  const items = (catalogue.data?.items ?? []).filter((s) => !exclude.includes(s.id));
  const full = picked.length >= max;
  const ready = picked.length >= min && picked.length <= max;

  return (
    <div className="space-y-3 text-sm">
      <div>
        <p id="picked-heading" className="font-medium">
          Chosen ({picked.length} of {min === max ? max : `${min} to ${max}`})
        </p>
        {picked.length === 0 ? (
          <p className="text-slate-600 dark:text-slate-400">Nothing chosen yet.</p>
        ) : (
          <ul aria-labelledby="picked-heading" className="flex flex-wrap gap-2 pt-1">
            {picked.map((p) => (
              <li key={p.id} className="flex items-center gap-1 rounded border border-slate-300 py-0.5 pl-2 dark:border-slate-700">
                <span className="break-all">{nameOf(p)}</span>
                <button type="button" className={linkButtonClass} aria-label={`Remove ${nameOf(p)}`} onClick={() => setPicked((prev) => prev.filter((x) => x.id !== p.id))}>
                  Remove
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
      <form role="search" aria-label="Series to compare" onSubmit={search} className="flex items-end gap-2">
        <label className="flex flex-col gap-1">
          Find series
          <input className={inputClass} placeholder="Name or external id" value={text} onChange={(e) => setText(e.target.value)} />
        </label>
        <button type="submit" className={buttonClass}>
          Search
        </button>
      </form>
      <ErrorLine error={catalogue.error} prefix="Could not load series" />
      {catalogue.isPending && <p>Loading series…</p>}
      {catalogue.isSuccess && items.length === 0 && <p className="text-slate-600 dark:text-slate-400">No series match.</p>}
      {items.length > 0 && (
        <ul aria-label="Catalogue" className="max-h-64 divide-y divide-slate-200 overflow-y-auto rounded border border-slate-200 dark:divide-slate-800 dark:border-slate-800">
          {items.map((s) => (
            <li key={s.id}>
              <label className="flex min-h-9 cursor-pointer items-center gap-2 px-2 py-1 hover:bg-slate-50 dark:hover:bg-slate-800">
                <input type="checkbox" checked={chosen(s.id)} disabled={full && !chosen(s.id)} onChange={() => toggle(s.id, s.name)} />
                <span className="font-medium">{s.name}</span>
                <span className="text-xs text-slate-600 dark:text-slate-400">{s.unit ?? "no unit"}</span>
              </label>
            </li>
          ))}
        </ul>
      )}
      {catalogue.data?.next_cursor && <p className="text-xs text-slate-600 dark:text-slate-400">More series match; search to narrow the list.</p>}
      <button type="button" className={buttonClass} disabled={!ready} onClick={() => onSubmit(picked.map((p) => p.id))}>
        {submitLabel}
      </button>
    </div>
  );
}
