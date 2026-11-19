import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState, type FormEvent } from "react";
import { buttonClass } from "../../components/Brand";
import {
  MAX_FILTER_NAME,
  inboxApi,
  inboxKeys,
  parseSearchString,
  savedFilterError,
  searchString,
  type InboxSearch,
  type SavedFilter,
} from "../../findingsInbox";
import { ErrorLine, dangerButtonClass, dialogs, inputClass, linkButtonClass, panelClass } from "../admin/ui";
import { fieldClass, mutedClass } from "./parts";

/** The caller's saved filters (spec 029): apply one, save the current search under a name,
 * delete one. They are personal, so every role may use them. */
export function SavedFilters({ search, onApply }: { search: InboxSearch; onApply: (search: InboxSearch) => void }) {
  const client = useQueryClient();
  const panelId = useId();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const filters = useQuery({ queryKey: inboxKeys.filters, queryFn: inboxApi.filters });
  const refresh = () => client.invalidateQueries({ queryKey: inboxKeys.filters });
  const save = useMutation({
    mutationFn: (as: string) => inboxApi.saveFilter(as, searchString(search)),
    onSuccess: () => {
      setName("");
      return refresh();
    },
  });
  const remove = useMutation({ mutationFn: (id: string) => inboxApi.deleteFilter(id), onSuccess: refresh });
  const items = filters.data?.items ?? [];
  const refused = savedFilterError(save.error);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (name.trim()) save.mutate(name.trim());
  };
  const confirmRemove = (filter: SavedFilter) => {
    if (dialogs.confirm(`Delete the saved filter "${filter.name}"?`)) remove.mutate(filter.id);
  };

  return (
    <div className="relative">
      <button type="button" className={buttonClass} aria-expanded={open} aria-controls={panelId} onClick={() => setOpen(!open)}>
        Saved filters{filters.data ? ` (${items.length})` : ""}
      </button>
      {open && (
        <section id={panelId} aria-label="Saved filters" className={`${panelClass} absolute right-0 z-20 mt-1 w-80 max-w-[90vw] space-y-3 text-sm shadow-lg`}>
          <ErrorLine error={filters.error} prefix="Could not load your filters" />
          {filters.isPending && <p className={mutedClass}>Loading…</p>}
          {filters.isSuccess && items.length === 0 && <p className={mutedClass}>No saved filters yet.</p>}
          {items.length > 0 && (
            <ul aria-label="Your filters" className="max-h-64 divide-y divide-slate-200 overflow-y-auto dark:divide-slate-800">
              {items.map((filter) => (
                <li key={filter.id} className="flex items-center justify-between gap-2 py-1">
                  <button
                    type="button"
                    className={`${linkButtonClass} min-w-0 truncate text-left`}
                    title={filter.query || "open and acknowledged, not grouped"}
                    onClick={() => {
                      onApply(parseSearchString(filter.query));
                      setOpen(false);
                    }}
                  >
                    {filter.name}
                  </button>
                  <button
                    type="button"
                    className={dangerButtonClass}
                    aria-label={`Delete ${filter.name}`}
                    disabled={remove.isPending}
                    onClick={() => confirmRemove(filter)}
                  >
                    Delete
                  </button>
                </li>
              ))}
            </ul>
          )}
          <ErrorLine error={remove.error} prefix="Could not delete the filter" />
          <form onSubmit={submit} className="flex items-end gap-2">
            <label className={`${fieldClass} min-w-0 flex-1`}>
              Save current filter as
              <input className={inputClass} value={name} maxLength={MAX_FILTER_NAME} onChange={(e) => setName(e.target.value)} />
            </label>
            <button type="submit" className={buttonClass} disabled={save.isPending || name.trim() === ""}>
              Save
            </button>
          </form>
          {refused ? (
            <p role="alert" className="text-red-700 dark:text-red-400">
              {refused}
            </p>
          ) : (
            <ErrorLine error={save.error} prefix="Could not save the filter" />
          )}
        </section>
      )}
    </div>
  );
}
