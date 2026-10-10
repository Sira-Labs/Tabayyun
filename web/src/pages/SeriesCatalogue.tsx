import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useState, type FormEvent } from "react";
import { buttonClass } from "../components/Brand";
import { formatLocalTime } from "../format";
import { seriesApi, sourcesApi, type SeriesFilters } from "../sources";
import { ErrorLine, inputClass, selectClass } from "./admin/ui";

export const SERIES_KINDS = ["measurement", "setpoint", "status", "counter"] as const;
export const SCORE_STEPS = [80, 60, 40] as const;

/** The catalogue's URL search: only known keys and values pass (spec 024). */
export function validateSeriesSearch(search: Record<string, unknown>): SeriesFilters {
  const out: SeriesFilters = {};
  const text = (v: unknown) => (typeof v === "string" && v.trim() !== "" ? v.trim().slice(0, 256) : undefined);
  out.q = text(search.q);
  out.source = text(search.source);
  out.unit = text(search.unit);
  const kind = text(search.kind);
  if (kind && (SERIES_KINDS as readonly string[]).includes(kind)) out.kind = kind;
  const score = Number(search.score);
  if ((SCORE_STEPS as readonly number[]).includes(score)) out.score = score;
  return Object.fromEntries(Object.entries(out).filter(([, v]) => v !== undefined)) as SeriesFilters;
}

/** `/series`: the series catalogue with search, filters in the URL, and "load more" (spec 024). */
export function SeriesCatalogue() {
  // The router merges the root's raw search into this route's: keep the known filters only.
  const filters = validateSeriesSearch(useSearch({ from: "/_app/series" }) as Record<string, unknown>);
  const navigate = useNavigate({ from: "/series" });
  const [text, setText] = useState(filters.q ?? "");
  const sources = useQuery({ queryKey: ["sources", "list"], queryFn: sourcesApi.list });
  const units = useQuery({ queryKey: ["series", "units"], queryFn: seriesApi.units });
  const series = useInfiniteQuery({
    queryKey: ["series", "list", filters],
    queryFn: ({ pageParam }) => seriesApi.list(filters, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const sourceNames = Object.fromEntries((sources.data?.items ?? []).map((s) => [s.id, s.name]));
  const items = series.data?.pages.flatMap((p) => p.items) ?? [];

  const apply = (change: Partial<SeriesFilters>) =>
    void navigate({ search: (prev: SeriesFilters) => validateSeriesSearch({ ...prev, ...change }) });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    apply({ q: text });
  };

  return (
    <section aria-labelledby="series-heading" className="space-y-4">
      <h1 id="series-heading" className="text-lg font-semibold">
        Series
      </h1>
      <div className="flex flex-wrap items-end gap-3">
        <form role="search" onSubmit={submit} className="flex items-end gap-2">
          <label className="flex flex-col gap-1 text-sm">
            Search
            <input className={inputClass} placeholder="Name or external id" value={text} onChange={(e) => setText(e.target.value)} />
          </label>
          <button type="submit" className={buttonClass}>
            Search
          </button>
        </form>
        <label className="flex flex-col gap-1 text-sm">
          Source
          <select className={selectClass} value={filters.source ?? ""} onChange={(e) => apply({ source: e.target.value })}>
            <option value="">All sources</option>
            {(sources.data?.items ?? []).map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Unit
          <select className={selectClass} value={filters.unit ?? ""} onChange={(e) => apply({ unit: e.target.value })}>
            <option value="">All units</option>
            {(units.data?.items ?? []).map((u) => (
              <option key={u.unit} value={u.unit}>
                {u.unit} ({u.n})
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Kind
          <select className={selectClass} value={filters.kind ?? ""} onChange={(e) => apply({ kind: e.target.value })}>
            <option value="">All kinds</option>
            {SERIES_KINDS.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Score
          <select
            className={selectClass}
            value={filters.score === undefined ? "" : String(filters.score)}
            onChange={(e) => apply({ score: e.target.value === "" ? undefined : Number(e.target.value) })}
          >
            <option value="">Any score</option>
            {SCORE_STEPS.map((s) => (
              <option key={s} value={s}>
                below {s}
              </option>
            ))}
          </select>
        </label>
      </div>
      {series.isPending && <p>Loading series…</p>}
      <ErrorLine error={series.error} prefix="Could not load series" />
      {series.isSuccess && items.length === 0 && <p className="text-slate-600 dark:text-slate-400">No series match.</p>}
      {items.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">Series by name</caption>
            <thead className="bg-slate-100 text-xs uppercase tracking-wide text-slate-700 dark:bg-slate-800 dark:text-slate-300">
              <tr>
                <th scope="col" className="p-2">Name</th>
                <th scope="col" className="hidden p-2 md:table-cell">External id</th>
                <th scope="col" className="hidden p-2 sm:table-cell">Source</th>
                <th scope="col" className="p-2">Unit</th>
                <th scope="col" className="hidden p-2 sm:table-cell">Kind</th>
                <th scope="col" className="p-2 text-right">Score</th>
                <th scope="col" className="hidden p-2 text-right sm:table-cell">Open findings</th>
                <th scope="col" className="hidden p-2 md:table-cell">Last run</th>
              </tr>
            </thead>
            <tbody>
              {items.map((s) => (
                <tr key={s.id} className="border-t border-slate-200 dark:border-slate-800">
                  <td className="p-2 font-medium">{s.name}</td>
                  <td className="hidden break-all p-2 font-mono text-xs md:table-cell">{s.external_id}</td>
                  <td className="hidden p-2 sm:table-cell">{sourceNames[s.source_id] ?? "—"}</td>
                  <td className="p-2">{s.unit ?? "—"}</td>
                  <td className="hidden p-2 sm:table-cell">{s.kind}</td>
                  <td className="p-2 text-right tabular-nums">{s.latest_score ? s.latest_score.overall.toFixed(1) : "—"}</td>
                  <td className="hidden p-2 text-right tabular-nums sm:table-cell">{s.open_findings}</td>
                  <td className="hidden p-2 text-xs md:table-cell">{s.last_run_at ? formatLocalTime(s.last_run_at) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {series.hasNextPage && (
        <button type="button" className={buttonClass} disabled={series.isFetchingNextPage} onClick={() => void series.fetchNextPage()}>
          Load more
        </button>
      )}
    </section>
  );
}
