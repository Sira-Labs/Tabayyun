import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { WINDOWS, daysOf, type OverviewSearch, type WindowDays } from "../../overview";
import { sourcesApi } from "../../sources";
import { inputClass, selectClass } from "../admin/ui";
import { fieldClass } from "../findings/parts";

/** The overview's filters (spec 032): window, source and asset prefix. The prefix goes into the
 * URL on Enter or when the field is left, not on every key. */
export function OverviewFilters({ search, onChange }: { search: OverviewSearch; onChange: (part: OverviewSearch) => void }) {
  const sources = useQuery({ queryKey: ["sources", "list"], queryFn: sourcesApi.list });
  const [asset, setAsset] = useState(search.asset ?? "");
  // Back and forward change the prefix while the page stays mounted: follow it in the field.
  const [shown, setShown] = useState(search.asset);
  if (search.asset !== shown) {
    setShown(search.asset);
    setAsset(search.asset ?? "");
  }
  const applyAsset = () => {
    const next = asset.trim() || undefined;
    if (next !== search.asset) onChange({ asset: next });
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    applyAsset();
  };

  return (
    <form role="search" aria-label="Overview filters" onSubmit={submit} className="flex flex-wrap items-end gap-3">
      <label className={fieldClass}>
        Window
        <select
          className={selectClass}
          value={daysOf(search)}
          onChange={(e) => onChange({ days: Number(e.target.value) as WindowDays })}
        >
          {WINDOWS.map((d) => (
            <option key={d} value={d}>
              Last {d} days
            </option>
          ))}
        </select>
      </label>
      <label className={fieldClass}>
        Source
        <select className={selectClass} value={search.source ?? ""} onChange={(e) => onChange({ source: e.target.value || undefined })}>
          <option value="">All sources</option>
          {(sources.data?.items ?? []).map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
          {/* A source in the URL that the list does not (yet) hold still shows as chosen. */}
          {search.source && sources.isSuccess && !sources.data.items.some((s) => s.id === search.source) && (
            <option value={search.source}>Unknown source</option>
          )}
        </select>
      </label>
      <label className={fieldClass}>
        Asset prefix
        <input
          className={`${inputClass} min-h-9`}
          value={asset}
          maxLength={200}
          placeholder="plant/"
          onChange={(e) => setAsset(e.target.value)}
          onBlur={applyAsset}
        />
      </label>
    </form>
  );
}
