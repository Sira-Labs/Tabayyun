import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { adminApi, type AuditEvent } from "../../admin";
import { buttonClass } from "../../components/Brand";
import { formatLocalTime } from "../../format";
import { ErrorLine, selectClass } from "./ui";

const AREAS = [
  { value: "", label: "All changes" },
  { value: "member.", label: "Members" },
  { value: "invitation.", label: "Invitations" },
  { value: "team.", label: "Teams" },
  { value: "workspace.", label: "Workspaces" },
  { value: "org.", label: "Organisation" },
];

/** `details` as short text: names first, then before → after. */
export function describeDetails(details: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(details)) {
    if (value === null || value === undefined || key === "invitation_id" || key === "invited_by") continue;
    if (key === "before" || key === "after") continue;
    if (typeof value === "object" && value && "before" in value && "after" in value) {
      const change = value as { before: unknown; after: unknown };
      parts.push(`${key}: ${String(change.before)} → ${String(change.after)}`);
    } else {
      parts.push(`${key}: ${String(value)}`);
    }
  }
  if ("before" in details || "after" in details) parts.push(`${String(details.before ?? "none")} → ${String(details.after ?? "none")}`);
  return parts.join(" · ");
}

/** Audit log tab: newest first, by area and workspace, with "Load more". Workspace admins who
 * are not org admins read the log of one of their workspaces. */
export function Audit({ isOrgAdmin }: { isOrgAdmin: boolean }) {
  const workspaces = useQuery({ queryKey: ["admin", "workspaces"], queryFn: adminApi.workspaces });
  const [area, setArea] = useState("");
  const [chosen, setChosen] = useState("");
  const workspaceId = chosen || (isOrgAdmin ? "" : (workspaces.data?.[0]?.id ?? ""));
  const ready = isOrgAdmin || workspaceId !== "";
  const events = useInfiniteQuery({
    queryKey: ["admin", "audit", area, workspaceId],
    queryFn: ({ pageParam }) => adminApi.audit({ action: area || undefined, workspace_id: workspaceId || undefined, before: pageParam }),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next,
    enabled: ready,
  });
  const items = events.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <section aria-labelledby="audit-heading" className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="audit-heading" className="font-semibold">
          Audit log
        </h2>
        <div className="flex flex-wrap gap-2">
          <select aria-label="Area" className={selectClass} value={area} onChange={(e) => setArea(e.target.value)}>
            {AREAS.map((a) => (
              <option key={a.value} value={a.value}>
                {a.label}
              </option>
            ))}
          </select>
          <select aria-label="Workspace" className={selectClass} value={workspaceId} onChange={(e) => setChosen(e.target.value)}>
            {isOrgAdmin && <option value="">Whole organisation</option>}
            {workspaces.data?.map((w) => (
              <option key={w.id} value={w.id}>
                {w.name}
              </option>
            ))}
          </select>
        </div>
      </div>
      <ErrorLine error={events.error} prefix="Could not load the audit log" />
      {events.isSuccess && items.length === 0 && <p className="text-slate-600 dark:text-slate-400">Nothing recorded yet.</p>}
      {items.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">Audit events, newest first</caption>
            <thead className="bg-slate-100 text-xs uppercase tracking-wide text-slate-700 dark:bg-slate-800 dark:text-slate-300">
              <tr>
                <th scope="col" className="p-2">When</th>
                <th scope="col" className="p-2">Who</th>
                <th scope="col" className="p-2">What</th>
                <th scope="col" className="hidden p-2 md:table-cell">IP</th>
              </tr>
            </thead>
            <tbody>
              {items.map((e: AuditEvent) => (
                <tr key={e.id} className="border-t border-slate-200 align-top dark:border-slate-800">
                  <td className="whitespace-nowrap p-2">{formatLocalTime(e.created_at)}</td>
                  <td className="p-2">{e.actor ? e.actor.email : "system"}</td>
                  <td className="p-2">
                    <span className="font-mono">{e.action}</span>
                    <div className="text-xs text-slate-600 dark:text-slate-400">{describeDetails(e.details)}</div>
                  </td>
                  <td className="hidden p-2 md:table-cell">{e.ip_address ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {events.hasNextPage && (
        <button type="button" className={buttonClass} disabled={events.isFetchingNextPage} onClick={() => void events.fetchNextPage()}>
          Load more
        </button>
      )}
    </section>
  );
}
