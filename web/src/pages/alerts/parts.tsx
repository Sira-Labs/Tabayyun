// Building blocks of the alerts pages (spec 031).
import { statusWords, type AlertNotification, type NotificationStatus } from "../../alerts";
import { formatLocalTime } from "../../format";

const STATUS_CLASS: Record<NotificationStatus, string> = {
  pending: "bg-slate-200 text-slate-900 dark:bg-slate-700 dark:text-slate-100",
  sending: "bg-sky-700 text-white",
  sent: "bg-emerald-700 text-white",
  failed: "bg-red-700 text-white",
  skipped: "bg-amber-300 text-slate-900",
};

/** A notification's status in words, as a coloured label. */
export function NotificationStatusBadge({ status }: { status: NotificationStatus }) {
  return <span className={`rounded px-2 py-0.5 text-xs font-medium ${STATUS_CLASS[status] ?? ""}`}>{statusWords(status)}</span>;
}

/** A notification as one line: status, when it was sent (or is due), and why it failed or was skipped. */
export function NotificationLine({ notification }: { notification: AlertNotification }) {
  const n = notification;
  return (
    <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1">
      <NotificationStatusBadge status={n.status} />
      <span>{formatLocalTime(n.sent_at ?? n.due_at)}</span>
      {n.error && (n.status === "failed" || n.status === "skipped") && (
        <span className="text-red-700 dark:text-red-400">{n.error}</span>
      )}
    </span>
  );
}
