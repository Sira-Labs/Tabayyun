import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { formatLocalTime } from "../../format";
import { atLeast, useWorkspaceRole } from "../../hooks/useWorkspaceRole";
import { suitesApi } from "../../suites";
import { ErrorLine } from "../admin/ui";
import { DatasetName, RunLink, ScheduleText, mutedClass, primaryLink } from "./parts";

/** `/suites`: the workspace's check suites with dataset, schedule, next and last run (spec 027). */
export function SuitesList() {
  const role = useWorkspaceRole();
  const suites = useQuery({ queryKey: ["suites", "list"], queryFn: suitesApi.listAll });
  const items = suites.data?.items ?? [];

  return (
    <section aria-labelledby="suites-heading" className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 id="suites-heading" className="text-lg font-semibold">
          Suites
        </h1>
        {atLeast(role, "editor") && (
          <Link to="/suites/new" className={primaryLink}>
            New suite
          </Link>
        )}
      </div>
      {suites.isPending && <p>Loading suites…</p>}
      <ErrorLine error={suites.error} prefix="Could not load suites" />
      {suites.isSuccess && items.length === 0 && (
        <p className={mutedClass}>No suites yet. A suite runs chosen checks on a dataset, on a schedule or on request.</p>
      )}
      {items.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">Suites by name</caption>
            <thead className="bg-slate-100 text-xs uppercase tracking-wide text-slate-700 dark:bg-slate-800 dark:text-slate-300">
              <tr>
                <th scope="col" className="p-2">Name</th>
                <th scope="col" className="p-2">Dataset</th>
                <th scope="col" className="p-2">Schedule</th>
                <th scope="col" className="hidden p-2 sm:table-cell">Next run</th>
                <th scope="col" className="p-2">Last run</th>
                <th scope="col" className="p-2">State</th>
              </tr>
            </thead>
            <tbody>
              {items.map((suite) => (
                <tr key={suite.id} className="border-t border-slate-200 dark:border-slate-800">
                  <td className="p-2">
                    <Link to="/suites/$suiteId" params={{ suiteId: suite.id }} className="font-medium underline-offset-2 hover:underline">
                      {suite.name}
                    </Link>
                  </td>
                  <td className="p-2">
                    <DatasetName suite={suite} />
                  </td>
                  <td className="p-2">
                    <ScheduleText schedule={suite.schedule} />
                  </td>
                  <td className="hidden p-2 text-xs sm:table-cell">{suite.next_run_at ? formatLocalTime(suite.next_run_at) : "—"}</td>
                  <td className="p-2 text-xs">{suite.last_run ? <RunLink run={suite.last_run} /> : "—"}</td>
                  <td className="p-2">{suite.enabled ? "enabled" : <span className={mutedClass}>disabled</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
