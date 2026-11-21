import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { alertKeys, alertsApi, inFlight, ruleWords, targetWords, throttleWords, type AlertRule } from "../../alerts";
import { ApiError } from "../../api";
import { buttonClass } from "../../components/Brand";
import { formatLocalTime } from "../../format";
import { useWorkspaceRole } from "../../hooks/useWorkspaceRole";
import type { Page } from "../../types";
import { ErrorLine, dangerButtonClass, dialogs, linkButtonClass, panelClass } from "../admin/ui";
import { mutedClass } from "../suites/parts";
import { NotificationStatusBadge } from "./parts";

/** `/alerts/:id`: a rule in words, its target and its notifications; admins also test, enable
 * or disable, edit and delete it (spec 031). */
export function AlertDetail() {
  const { alertId } = useParams({ from: "/_app/alerts/$alertId" });
  const role = useWorkspaceRole();
  const rule = useQuery({ queryKey: alertKeys.detail(alertId), queryFn: () => alertsApi.get(alertId) });

  if (rule.isPending) return <p>Loading rule…</p>;
  if (rule.isError) {
    const missing = rule.error instanceof ApiError && rule.error.status === 404;
    return missing ? <p role="alert">This alert rule does not exist (any more).</p> : <ErrorLine error={rule.error} prefix="Could not load the rule" />;
  }
  const r = rule.data;
  return (
    <section aria-labelledby="alert-heading" className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 id="alert-heading" className="flex flex-wrap items-baseline gap-2 text-lg font-semibold">
          {r.name}
          {!r.enabled && <span className={`text-sm font-normal ${mutedClass}`}>(disabled)</span>}
        </h1>
        <Link to="/alerts" className="text-sm underline">
          All alerts
        </Link>
      </div>
      <Configuration rule={r} />
      {role === "admin" && <Actions rule={r} />}
      <Notifications ruleId={r.id} />
    </section>
  );
}

/** What the rule watches, where it sends and how often. */
function Configuration({ rule }: { rule: AlertRule }) {
  const target = rule.channel === "webhook" && rule.url ? rule.url : targetWords(rule);
  const rows: [string, React.ReactNode][] = [
    ["Watches", ruleWords(rule)],
    ["Channel", rule.channel],
    [rule.channel === "email" ? "Recipients" : "Webhook", <span className="break-all">{target}</span>],
    ...(rule.channel === "webhook" ? [["Signing", rule.has_secret ? "signed with a secret" : "not signed"] as [string, React.ReactNode]] : []),
    ["Throttle", throttleWords(rule.throttle_minutes)],
    ["State", rule.enabled ? "enabled" : "disabled: not evaluated after runs"],
  ];
  return (
    <section aria-labelledby="alert-config-heading" className={`${panelClass} space-y-2`}>
      <h2 id="alert-config-heading" className="font-semibold">
        Rule
      </h2>
      <dl className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[8rem_1fr]">
        {rows.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className={mutedClass}>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

/** Send test, enable or disable, edit and delete (workspace admins). */
function Actions({ rule }: { rule: AlertRule }) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const refresh = () => {
    void client.invalidateQueries({ queryKey: alertKeys.detail(rule.id) });
    void client.invalidateQueries({ queryKey: alertKeys.list });
  };
  const test = useMutation({
    mutationFn: () => alertsApi.test(rule.id),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: alertKeys.notifications(rule.id) });
      void client.invalidateQueries({ queryKey: alertKeys.list });
    },
  });
  const toggle = useMutation({ mutationFn: () => alertsApi.patch(rule.id, { enabled: !rule.enabled }), onSuccess: refresh });
  const remove = useMutation({
    mutationFn: () => alertsApi.remove(rule.id),
    onSuccess: () => {
      client.removeQueries({ queryKey: alertKeys.detail(rule.id) });
      client.removeQueries({ queryKey: alertKeys.notifications(rule.id) });
      // The list must not show the deleted rule while it reloads.
      client.setQueryData<Page<AlertRule>>(alertKeys.list, (page) => page && { ...page, items: page.items.filter((row) => row.id !== rule.id) });
      void client.invalidateQueries({ queryKey: alertKeys.list });
      void navigate({ to: "/alerts" });
    },
  });

  return (
    <section aria-labelledby="alert-actions-heading" className={`${panelClass} space-y-3`}>
      <h2 id="alert-actions-heading" className="font-semibold">
        Actions
      </h2>
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" className={buttonClass} disabled={test.isPending} onClick={() => test.mutate()}>
          Send test
        </button>
        <Link to="/alerts/$alertId/edit" params={{ alertId: rule.id }} className={buttonClass}>
          Edit
        </Link>
        <button type="button" className={linkButtonClass} disabled={toggle.isPending} onClick={() => toggle.mutate()}>
          {rule.enabled ? "Disable" : "Enable"}
        </button>
        <button
          type="button"
          className={dangerButtonClass}
          disabled={remove.isPending}
          onClick={() => dialogs.confirm(`Delete the alert rule "${rule.name}"? Its notifications are deleted with it.`) && remove.mutate()}
        >
          Delete
        </button>
      </div>
      <p role="status" className="text-sm">
        {test.isSuccess ? "Test notification queued." : ""}
      </p>
      <ErrorLine error={test.error} prefix="Could not send a test" />
      <ErrorLine error={toggle.error} prefix={`Could not ${rule.enabled ? "disable" : "enable"} the rule`} />
      <ErrorLine error={remove.error} prefix="Could not delete the rule" />
    </section>
  );
}

/** The rule's notifications, newest first; polled while one is waiting or being sent. */
function Notifications({ ruleId }: { ruleId: string }) {
  const notifications = useQuery({
    queryKey: alertKeys.notifications(ruleId),
    queryFn: () => alertsApi.notifications(ruleId),
    refetchInterval: (q) => (q.state.data?.items.some(inFlight) ? 5000 : false),
  });
  const items = notifications.data?.items ?? [];
  return (
    <section aria-labelledby="alert-notifications-heading" className={`${panelClass} space-y-2`}>
      <h2 id="alert-notifications-heading" className="font-semibold">
        Notifications
      </h2>
      {notifications.isPending && <p>Loading notifications…</p>}
      <ErrorLine error={notifications.error} prefix="Could not load the notifications" />
      {notifications.isSuccess && items.length === 0 && (
        <p className={`text-sm ${mutedClass}`}>No notifications yet. One is made when a run matches the rule, or with "Send test".</p>
      )}
      {items.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">The rule's notifications, newest first</caption>
            <thead className={`text-xs uppercase tracking-wide ${mutedClass}`}>
              <tr>
                <th scope="col" className="p-1">Due</th>
                <th scope="col" className="p-1">Kind</th>
                <th scope="col" className="p-1">Status</th>
                <th scope="col" className="p-1 text-right">Matches</th>
                <th scope="col" className="p-1 text-right">Attempts</th>
                <th scope="col" className="p-1">Error</th>
              </tr>
            </thead>
            <tbody>
              {items.map((n) => (
                <tr key={n.id} className="border-t border-slate-200 align-top dark:border-slate-800">
                  <td className="p-1 text-xs sm:whitespace-nowrap">{formatLocalTime(n.due_at)}</td>
                  <td className="p-1 text-xs">{n.kind}</td>
                  <td className="p-1">
                    <NotificationStatusBadge status={n.status} />
                  </td>
                  <td className="p-1 text-right tabular-nums">{n.n_matches}</td>
                  <td className="p-1 text-right tabular-nums">{n.attempts}</td>
                  <td className="p-1 text-xs text-red-700 dark:text-red-400">{n.error ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
