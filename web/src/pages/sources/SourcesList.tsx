import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { formatLocalTime } from "../../format";
import { atLeast, useWorkspaceRole } from "../../hooks/useWorkspaceRole";
import { TYPE_LABEL, sourcesApi } from "../../sources";
import { ErrorLine } from "../admin/ui";
import { HealthBadge, JobBadge } from "./parts";

const primaryLink =
  "min-h-11 rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white dark:bg-slate-100 dark:text-slate-900";

/** `/sources`: the workspace's sources with health and last fetch (spec 024). */
export function SourcesList() {
  const role = useWorkspaceRole();
  const sources = useQuery({ queryKey: ["sources", "list"], queryFn: sourcesApi.list });
  const items = sources.data?.items ?? [];

  return (
    <section aria-labelledby="sources-heading" className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 id="sources-heading" className="text-lg font-semibold">
          Sources
        </h1>
        {atLeast(role, "admin") && (
          <Link to="/sources/new" className={primaryLink}>
            New source
          </Link>
        )}
      </div>
      {sources.isPending && <p>Loading sources…</p>}
      <ErrorLine error={sources.error} prefix="Could not load sources" />
      {sources.isSuccess && items.length === 0 && <p className="text-slate-600 dark:text-slate-400">No sources yet.</p>}
      {items.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">Sources by name</caption>
            <thead className="bg-slate-100 text-xs uppercase tracking-wide text-slate-700 dark:bg-slate-800 dark:text-slate-300">
              <tr>
                <th scope="col" className="p-2">Name</th>
                <th scope="col" className="p-2">Type</th>
                <th scope="col" className="p-2">Health</th>
                <th scope="col" className="hidden p-2 text-right sm:table-cell">Series</th>
                <th scope="col" className="p-2">Last fetch</th>
              </tr>
            </thead>
            <tbody>
              {items.map((source) => {
                const last = source.last_fetch;
                return (
                  <tr key={source.id} className="border-t border-slate-200 dark:border-slate-800">
                    <td className="p-2">
                      {source.connector ? (
                        <Link to="/sources/$sourceId" params={{ sourceId: source.id }} className="font-medium underline-offset-2 hover:underline">
                          {source.name}
                        </Link>
                      ) : (
                        <span className="font-medium">{source.name}</span>
                      )}
                      {!source.enabled && <span className="ml-2 text-xs text-slate-600 dark:text-slate-400">(disabled)</span>}
                    </td>
                    <td className="p-2">{TYPE_LABEL[source.type] ?? source.type}</td>
                    <td className="p-2">{source.connector ? <HealthBadge status={source.health_status} /> : "—"}</td>
                    <td className="hidden p-2 text-right tabular-nums sm:table-cell">{source.n_series}</td>
                    <td className="p-2 text-xs">
                      {last ? (
                        <span className="flex flex-wrap items-center gap-2">
                          <JobBadge status={last.status} /> {formatLocalTime(last.finished_at)}
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
