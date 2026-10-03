// The admin API (spec 014): org, members, invitations, teams, workspaces and the audit log.
// Every call needs a passkey sign-in from the last 12 hours; `isPasskeyRequired` recognises
// the answer when it is missing.
import { ApiError, apiGet, apiSend } from "./api";
import type { WorkspaceRole } from "./workspace";

export type OrgRole = "owner" | "admin" | "member";
export type Person = { user_id: string; email: string; display_name: string };

export type AdminOrg = { id: string; name: string; created_at: string; role: OrgRole };
export type Member = Person & { role: OrgRole; joined_at: string; disabled: boolean };
export type MemberPage = { items: Member[]; next: string | null };

export type InvitationStatus = "pending" | "accepted" | "revoked" | "expired";
export type Invitation = {
  id: string;
  email: string;
  org_role: OrgRole;
  workspace: { id: string; name: string } | null;
  workspace_role: WorkspaceRole | null;
  invited_by: Person | null;
  created_at: string;
  expires_at: string;
  status: InvitationStatus;
  email_status: "not_configured" | "queued" | "sent" | "failed";
  email_error: string | null;
};
export type InvitationIn = {
  email: string;
  org_role: "admin" | "member";
  workspace_id?: string;
  workspace_role?: WorkspaceRole;
};

export type Team = {
  id: string;
  name: string;
  members: Person[];
  workspaces: { workspace_id: string; name: string; role: WorkspaceRole }[];
};

export type AdminWorkspace = { id: string; name: string; timezone: string; created_at: string; role: WorkspaceRole };
export type Access = {
  members: (Person & { role: WorkspaceRole })[];
  teams: { team_id: string; name: string; role: WorkspaceRole }[];
  org_admins: (Person & { role: OrgRole })[];
};

export type AuditEvent = {
  id: string;
  created_at: string;
  actor: Person | null;
  action: string;
  target_type: string;
  target_id: string;
  workspace_id: string | null;
  details: Record<string, unknown>;
  ip_address: string | null;
};
export type AuditPage = { items: AuditEvent[]; next: string | null };

export const ORG_ROLES: OrgRole[] = ["owner", "admin", "member"];
export const WORKSPACE_ROLES: WorkspaceRole[] = ["admin", "editor", "viewer"];
export const ORG_ADMIN_ROLES = new Set<string>(["owner", "admin"]);

/** Whether the server asked for a fresh passkey sign-in (403 `second-factor-required`). */
export function isPasskeyRequired(error: unknown): boolean {
  return error instanceof ApiError && error.status === 403 && error.message === "second-factor-required";
}

/** Readable text for the codes admin routes answer with. */
export const ERROR_TEXT: Record<string, string> = {
  last_owner: "The organisation needs at least one owner. Make someone else owner first.",
  forbidden: "Your role does not allow this.",
  name_taken: "That name is already in use.",
  invalid_name: "Names are 1 to 100 characters.",
  invalid_timezone: "Use a time zone name such as Europe/Berlin.",
  invalid_email: "That does not look like an email address.",
  already_member: "That person is already a member.",
  invitation_pending: "There is already an open invitation for this email; resend it instead.",
  not_pending: "That invitation is no longer open.",
  default_workspace: "The default workspace cannot be deleted.",
  workspace_not_empty: "The workspace still holds data; it can only be deleted when empty.",
  "second-factor-required": "Sign in with a passkey again: admin actions need one from the last 12 hours.",
  "not found": "It no longer exists, or you cannot see it.",
};

/** A user-facing message for a failed admin call. */
export function adminErrorText(error: unknown): string {
  if (error instanceof ApiError) return ERROR_TEXT[error.message] ?? error.message;
  return error instanceof Error ? error.message : String(error);
}

function q(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  const s = search.toString();
  return s ? `?${s}` : "";
}

const id = encodeURIComponent;

export const adminApi = {
  org: () => apiGet<AdminOrg>("/api/admin/org"),
  renameOrg: (name: string) => apiSend<AdminOrg>("PATCH", "/api/admin/org", { name }),

  members: (params: { q?: string; cursor?: string | null; limit?: number } = {}) =>
    apiGet<MemberPage>(`/api/admin/members${q(params)}`),
  setMemberRole: (userId: string, role: OrgRole) => apiSend<Member>("PATCH", `/api/admin/members/${id(userId)}`, { role }),
  removeMember: (userId: string) => apiSend<void>("DELETE", `/api/admin/members/${id(userId)}`),

  invitations: (status: "pending" | "all" = "pending") => apiGet<Invitation[]>(`/api/admin/invitations${q({ status })}`),
  invite: (body: InvitationIn) => apiSend<Invitation>("POST", "/api/admin/invitations", body),
  resendInvitation: (invitationId: string) => apiSend<Invitation>("POST", `/api/admin/invitations/${id(invitationId)}/resend`),
  revokeInvitation: (invitationId: string) => apiSend<void>("DELETE", `/api/admin/invitations/${id(invitationId)}`),

  teams: () => apiGet<Team[]>("/api/admin/teams"),
  createTeam: (name: string) => apiSend<Team>("POST", "/api/admin/teams", { name }),
  renameTeam: (teamId: string, name: string) => apiSend<Team>("PATCH", `/api/admin/teams/${id(teamId)}`, { name }),
  deleteTeam: (teamId: string) => apiSend<void>("DELETE", `/api/admin/teams/${id(teamId)}`),
  addTeamMember: (teamId: string, userId: string) => apiSend<void>("PUT", `/api/admin/teams/${id(teamId)}/members/${id(userId)}`),
  removeTeamMember: (teamId: string, userId: string) =>
    apiSend<void>("DELETE", `/api/admin/teams/${id(teamId)}/members/${id(userId)}`),

  workspaces: () => apiGet<AdminWorkspace[]>("/api/admin/workspaces"),
  createWorkspace: (name: string, timezone: string) =>
    apiSend<AdminWorkspace>("POST", "/api/admin/workspaces", { name, timezone }),
  updateWorkspace: (workspaceId: string, body: { name?: string; timezone?: string }) =>
    apiSend<AdminWorkspace>("PATCH", `/api/admin/workspaces/${id(workspaceId)}`, body),
  deleteWorkspace: (workspaceId: string) => apiSend<void>("DELETE", `/api/admin/workspaces/${id(workspaceId)}`),
  access: (workspaceId: string) => apiGet<Access>(`/api/admin/workspaces/${id(workspaceId)}/access`),
  setWorkspaceMember: (workspaceId: string, userId: string, role: WorkspaceRole) =>
    apiSend<void>("PUT", `/api/admin/workspaces/${id(workspaceId)}/members/${id(userId)}`, { role }),
  removeWorkspaceMember: (workspaceId: string, userId: string) =>
    apiSend<void>("DELETE", `/api/admin/workspaces/${id(workspaceId)}/members/${id(userId)}`),
  setWorkspaceTeam: (workspaceId: string, teamId: string, role: WorkspaceRole) =>
    apiSend<void>("PUT", `/api/admin/workspaces/${id(workspaceId)}/teams/${id(teamId)}`, { role }),
  removeWorkspaceTeam: (workspaceId: string, teamId: string) =>
    apiSend<void>("DELETE", `/api/admin/workspaces/${id(workspaceId)}/teams/${id(teamId)}`),

  audit: (params: { workspace_id?: string; action?: string; before?: string | null } = {}) =>
    apiGet<AuditPage>(`/api/admin/audit${q({ ...params, limit: 50 })}`),
};
