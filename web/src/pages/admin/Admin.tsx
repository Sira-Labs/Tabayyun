import { useQuery } from "@tanstack/react-query";
import { Link, getRouteApi } from "@tanstack/react-router";
import { ORG_ADMIN_ROLES, adminApi, adminErrorText, isPasskeyRequired, type OrgRole } from "../../admin";
import { authApi } from "../../auth";
import { Audit } from "./Audit";
import { Members } from "./Members";
import { PasskeyGate } from "./PasskeyGate";
import { Teams } from "./Teams";
import { Workspaces } from "./Workspaces";

export type AdminTab = "members" | "workspaces" | "teams" | "audit";
export const ADMIN_TABS: AdminTab[] = ["members", "workspaces", "teams", "audit"];
const TAB_LABELS: Record<AdminTab, string> = { members: "Members", workspaces: "Workspaces", teams: "Teams", audit: "Audit log" };

const route = getRouteApi("/_app/admin");

const tabClass = "rounded px-3 py-2 text-sm font-medium hover:bg-slate-200 dark:hover:bg-slate-800";
const tabActive = "bg-slate-200 text-slate-900 dark:bg-slate-800 dark:text-white";

/** `/admin` (spec 014): members and invitations, workspaces and their access, teams, and the
 * audit log. Org admins see every tab; workspace admins see their workspaces and its log. A
 * missing passkey sign-in shows the gate instead. */
export function Admin() {
  const { tab } = route.useSearch();
  const me = useQuery({ queryKey: ["me"], queryFn: authApi.me, retry: false, staleTime: 60_000 });
  const role = me.data?.role as OrgRole | undefined;
  const isOrgAdmin = role !== undefined && ORG_ADMIN_ROLES.has(role);
  // One cheap call first: it answers `second-factor-required` before any tab renders.
  const probe = useQuery({
    queryKey: ["admin", "probe", isOrgAdmin],
    queryFn: async (): Promise<unknown> => (isOrgAdmin ? adminApi.org() : adminApi.workspaces()),
    enabled: me.isSuccess,
    retry: false,
  });
  const tabs = isOrgAdmin ? ADMIN_TABS : (["workspaces", "audit"] as AdminTab[]);
  const current = tabs.includes(tab ?? "members") ? (tab ?? tabs[0]) : tabs[0];

  return (
    <section aria-labelledby="admin-heading" className="space-y-4">
      <h1 id="admin-heading" className="text-lg font-semibold">
        Admin
      </h1>
      {(me.isPending || probe.isPending) && <p>Loading…</p>}
      {probe.isError && isPasskeyRequired(probe.error) && <PasskeyGate />}
      {probe.isError && !isPasskeyRequired(probe.error) && (
        <p role="alert" className="text-red-700 dark:text-red-400">
          {adminErrorText(probe.error)}
        </p>
      )}
      {probe.isSuccess && role && (
        <>
          <nav aria-label="Admin sections" className="flex flex-wrap gap-1">
            {tabs.map((t) => (
              <Link
                key={t}
                to="/admin"
                search={{ tab: t }}
                className={`${tabClass} ${t === current ? tabActive : ""}`}
                aria-current={t === current ? "page" : undefined}
              >
                {TAB_LABELS[t]}
              </Link>
            ))}
          </nav>
          {current === "members" && <Members myRole={role} />}
          {current === "workspaces" && <Workspaces isOrgAdmin={isOrgAdmin} isOwner={role === "owner"} />}
          {current === "teams" && <Teams />}
          {current === "audit" && <Audit isOrgAdmin={isOrgAdmin} />}
        </>
      )}
    </section>
  );
}
