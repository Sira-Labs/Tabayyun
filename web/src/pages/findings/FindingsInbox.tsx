import { useNavigate, useSearch } from "@tanstack/react-router";
import { useState } from "react";
import { buttonClass } from "../../components/Brand";
import { GROUPINGS, exportUrl, inboxApi, listUrl, validateInboxSearch, type Grouping, type InboxSearch } from "../../findingsInbox";
import { atLeast, useWorkspaceRole } from "../../hooks/useWorkspaceRole";
import { ErrorLine, selectClass } from "../admin/ui";
import { FilterBar } from "./FilterBar";
import { FindingsList } from "./FindingsList";
import { Groups, groupLabel } from "./Groups";
import { SavedFilters } from "./SavedFilters";
import { fieldClass } from "./parts";

const GROUP_LABEL: Record<Grouping, string> = { check: "Check", series: "Series", asset: "Asset" };

/** Offer a file as a download. */
function save(name: string, blob: Blob) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

/** `/findings`: the workspace's findings across all series, filtered through the URL, grouped,
 * selected and worked through (spec 029). */
export function FindingsInbox() {
  // The router merges the root's raw search into this route's: keep the known keys only.
  const search = validateInboxSearch(useSearch({ from: "/_app/findings" }) as Record<string, unknown>);
  const navigate = useNavigate({ from: "/findings" });
  const canWrite = atLeast(useWorkspaceRole(), "editor");
  const [notice, setNotice] = useState("");
  const [exportError, setExportError] = useState<unknown>(null);

  const go = (next: (prev: InboxSearch) => InboxSearch, replace = false) => {
    setNotice("");
    setExportError(null);
    void navigate({ search: (prev: InboxSearch) => validateInboxSearch(next(prev)), replace });
  };
  // A changed filter or grouping has other groups: none is open.
  const change = (part: Partial<InboxSearch>, options?: { replace?: boolean }) =>
    go((prev) => ({ ...prev, ...part, open: undefined }), options?.replace);

  // The link's own request cannot name the workspace, so the click fetches the file with the
  // app's headers; the href stays for "copy link" and for opening it without the app.
  const exportCsv = (e: React.MouseEvent) => {
    e.preventDefault();
    setExportError(null);
    inboxApi.exportCsv(search).then(
      ({ blob, truncated }) => {
        save("findings.csv", blob);
        if (truncated) setNotice("The export holds the newest 50,000 findings; the list is longer. Narrow the filter to export the rest.");
      },
      (error: unknown) => setExportError(error),
    );
  };

  return (
    <section aria-labelledby="findings-heading" className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 id="findings-heading" className="text-lg font-semibold">
          Findings
        </h1>
        <div className="flex flex-wrap items-center gap-2">
          <SavedFilters search={search} onApply={(saved) => go(() => saved)} />
          <a href={exportUrl(search)} download="findings.csv" className={buttonClass} onClick={exportCsv}>
            Export CSV
          </a>
        </div>
      </div>
      <FilterBar search={search} onChange={change} />
      <label className={`${fieldClass} w-fit`}>
        Group by
        <select className={selectClass} value={search.group ?? ""} onChange={(e) => change({ group: (e.target.value || undefined) as Grouping | undefined })}>
          <option value="">No grouping</option>
          {GROUPINGS.map((g) => (
            <option key={g} value={g}>
              {GROUP_LABEL[g]}
            </option>
          ))}
        </select>
      </label>
      <ErrorLine error={exportError} prefix="Could not export the findings" />
      <p role="status" aria-live="polite" className={notice ? "text-sm" : "sr-only"}>
        {notice}
      </p>
      {search.group ? (
        <Groups search={search} by={search.group} openKey={search.open} onOpen={(open) => go((prev) => ({ ...prev, open }))}>
          {(group) => {
            const filter = { by: search.group as Grouping, key: group.key };
            return (
              <FindingsList
                key={listUrl(search, filter)}
                search={search}
                group={filter}
                label={`Findings of ${groupLabel(filter.by, group)}`}
                canWrite={canWrite}
                onNotice={setNotice}
              />
            );
          }}
        </Groups>
      ) : (
        <FindingsList key={listUrl(search, null)} search={search} group={null} label="Findings" canWrite={canWrite} onNotice={setNotice} />
      )}
    </section>
  );
}
