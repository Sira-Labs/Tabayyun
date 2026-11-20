import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { alertKeys, alertsApi, ruleWords, targetWords, throttleWords } from "../../alerts";
import { useWorkspaceRole } from "../../hooks/useWorkspaceRole";
import { ErrorLine } from "../admin/ui";
import { mutedClass, primaryLink } from "../suites/parts";
import { NotificationLine } from "./parts";

/** `/alerts`: the workspace's alert rules with what they watch, where they send and their
 * last notification (spec 031). */
export function AlertsList() {
  const role = useWorkspaceRole();
  const rules = useQuery({ queryKey: alertKeys.list, queryFn: alertsApi.list });
  const items = rules.data?.items ?? [];

  return (
    <section aria-labelledby="alerts-heading" className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 id="alerts-heading" className="text-lg font-semibold">
          Alerts
        </h1>
        {role === "admin" && (
          <Link to="/alerts/new" className={primaryLink}>
            New rule
          </Link>
        )}
      </div>
      {rules.isPending && <p>Loading alert rules…</p>}
      <ErrorLine error={rules.error} prefix="Could not load alert rules" />
      {rules.isSuccess && items.length === 0 && (
        <p className={mutedClass}>
          No alert rules yet. A rule sends an email or calls a webhook when a run finds new findings or a score falls below a
          threshold.
        </p>
      )}
      {items.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">Alert rules by name</caption>
            <thead className="bg-slate-100 text-xs uppercase tracking-wide text-slate-700 dark:bg-slate-800 dark:text-slate-300">
              <tr>
                <th scope="col" className="p-2">Name</th>
                <th scope="col" className="p-2">Watches</th>
                <th scope="col" className="p-2">Sends to</th>
                <th scope="col" className="hidden p-2 sm:table-cell">Throttle</th>
                <th scope="col" className="p-2">State</th>
                <th scope="col" className="p-2">Last notification</th>
              </tr>
            </thead>
            <tbody>
              {items.map((rule) => (
                <tr key={rule.id} className="border-t border-slate-200 align-top dark:border-slate-800">
                  <td className="p-2">
                    <Link to="/alerts/$alertId" params={{ alertId: rule.id }} className="font-medium underline-offset-2 hover:underline">
                      {rule.name}
                    </Link>
                  </td>
                  <td className="p-2">{ruleWords(rule)}</td>
                  <td className="p-2 break-all">
                    {rule.channel}: {targetWords(rule)}
                  </td>
                  <td className="hidden p-2 sm:table-cell">{throttleWords(rule.throttle_minutes)}</td>
                  <td className="p-2">{rule.enabled ? "enabled" : <span className={mutedClass}>disabled</span>}</td>
                  <td className="p-2 text-xs">{rule.last_notification ? <NotificationLine notification={rule.last_notification} /> : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
