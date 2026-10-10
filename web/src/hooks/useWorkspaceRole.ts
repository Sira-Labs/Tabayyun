import { useQuery } from "@tanstack/react-query";
import { ORG_ADMIN_ROLES } from "../admin";
import { api } from "../api";
import { authApi } from "../auth";
import { currentWorkspaceId, resolveWorkspace, type WorkspaceRole } from "../workspace";

const RANK: Record<WorkspaceRole, number> = { viewer: 0, editor: 1, admin: 2 };

/** The caller's role in the current workspace (spec 014); org owners and admins act as admins,
 * as on the server. Null while it is loading. */
export function useWorkspaceRole(): WorkspaceRole | null {
  const me = useQuery({ queryKey: ["me"], queryFn: authApi.me, retry: false, staleTime: 60_000 });
  const workspaces = useQuery({ queryKey: ["workspaces"], queryFn: api.listWorkspaces, staleTime: 60_000 });
  if (!me.data || !workspaces.data) return null;
  if (ORG_ADMIN_ROLES.has(me.data.role)) return "admin";
  return resolveWorkspace(workspaces.data, currentWorkspaceId())?.role ?? null;
}

/** Whether `role` is at least `needed`. */
export function atLeast(role: WorkspaceRole | null, needed: WorkspaceRole): boolean {
  return role !== null && RANK[role] >= RANK[needed];
}
