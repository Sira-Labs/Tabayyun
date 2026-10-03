// An in-memory admin API (spec 014) for the panel tests: enough state to see a change land.
import type { Access, AdminWorkspace, AuditEvent, Invitation, Member, Team } from "../admin";
import { json, type Route } from "./helpers";

export type AdminState = {
  members: Member[];
  invitations: Invitation[];
  teams: Team[];
  workspaces: AdminWorkspace[];
  access: Record<string, Access>;
  audit: AuditEvent[];
  requests: { method: string; url: string; body: unknown }[];
};

const person = (id: string, email: string) => ({ user_id: id, email, display_name: email.split("@")[0] ?? email });

export function adminState(): AdminState {
  return {
    members: [
      { ...person("u1", "ana@example.org"), role: "owner", joined_at: "2026-10-01T00:00:00Z", disabled: false },
      { ...person("u2", "mia@example.org"), role: "member", joined_at: "2026-10-02T00:00:00Z", disabled: false },
    ],
    invitations: [],
    teams: [{ id: "t1", name: "ops", members: [], workspaces: [] }],
    workspaces: [{ id: "w1", name: "default", timezone: "UTC", created_at: "2026-10-01T00:00:00Z", role: "admin" }],
    access: { w1: { members: [], teams: [], org_admins: [{ ...person("u1", "ana@example.org"), role: "owner" }] } },
    audit: [],
    requests: [],
  };
}

/** A route function serving the admin API from `state`; `gate` answers every call with the
 * passkey refusal. */
export function adminRoute(state: AdminState, { gate = false } = {}): Route {
  return (url, init) => {
    const method = init?.method ?? "GET";
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
    if (method !== "GET") state.requests.push({ method, url, body });
    if (gate && url.startsWith("/api/admin")) return json({ detail: "second-factor-required" }, 403);
    const path = url.split("?")[0] ?? url;
    const seg = path.split("/").slice(3); // after /api/admin
    if (url.startsWith("/api/runs")) return { items: [], next_cursor: null };
    if (path === "/api/admin/org") {
      return method === "PATCH"
        ? { id: "o1", name: body.name, created_at: "2026-10-01T00:00:00Z", role: "owner" }
        : { id: "o1", name: "default", created_at: "2026-10-01T00:00:00Z", role: "owner" };
    }
    if (seg[0] === "members") {
      if (method === "GET") return { items: state.members, next: null };
      const member = state.members.find((m) => m.user_id === seg[1])!;
      if (method === "PATCH") {
        member.role = body.role;
        return member;
      }
      state.members = state.members.filter((m) => m !== member);
      return new Response(null, { status: 204 });
    }
    if (seg[0] === "invitations") {
      if (method === "GET") return state.invitations;
      if (method === "POST" && seg.length === 1) {
        const workspace = state.workspaces.find((w) => w.id === body.workspace_id);
        const invitation: Invitation = {
          id: `i${state.invitations.length + 1}`,
          email: body.email,
          org_role: body.org_role,
          workspace: workspace ? { id: workspace.id, name: workspace.name } : null,
          workspace_role: body.workspace_role ?? null,
          invited_by: person("u1", "ana@example.org"),
          created_at: "2026-10-03T10:00:00Z",
          expires_at: "2026-10-17T10:00:00Z",
          status: "pending",
          email_status: "queued",
          email_error: null,
        };
        state.invitations.unshift(invitation);
        return json(invitation, 201);
      }
      const invitation = state.invitations.find((i) => i.id === seg[1])!;
      if (seg[2] === "resend") return invitation;
      invitation.status = "revoked";
      return new Response(null, { status: 204 });
    }
    if (seg[0] === "teams") {
      if (method === "GET") return state.teams;
      if (method === "POST") {
        const team: Team = { id: `t${state.teams.length + 1}`, name: body.name, members: [], workspaces: [] };
        state.teams.push(team);
        return json(team, 201);
      }
      const team = state.teams.find((t) => t.id === seg[1])!;
      if (seg[2] === "members") {
        const member = state.members.find((m) => m.user_id === seg[3])!;
        if (method === "PUT") team.members.push(person(member.user_id, member.email));
        else team.members = team.members.filter((m) => m.user_id !== seg[3]);
        return new Response(null, { status: 204 });
      }
      if (method === "PATCH") {
        team.name = body.name;
        return team;
      }
      state.teams = state.teams.filter((t) => t !== team);
      return new Response(null, { status: 204 });
    }
    if (seg[0] === "workspaces") {
      if (seg.length === 1) {
        if (method === "GET") return state.workspaces;
        const workspace: AdminWorkspace = { id: "w2", name: body.name, timezone: body.timezone, created_at: "2026-10-03T00:00:00Z", role: "admin" };
        state.workspaces.push(workspace);
        state.access.w2 = { members: [], teams: [], org_admins: [] };
        return json(workspace, 201);
      }
      const workspace = state.workspaces.find((w) => w.id === seg[1])!;
      const access = state.access[seg[1] ?? ""]!;
      if (seg[2] === "access") return access;
      if (seg[2] === "members") {
        const member = state.members.find((m) => m.user_id === seg[3])!;
        access.members = access.members.filter((m) => m.user_id !== seg[3]);
        if (method === "PUT") access.members.push({ ...person(member.user_id, member.email), role: body.role });
        return new Response(null, { status: 204 });
      }
      if (seg[2] === "teams") {
        const team = state.teams.find((t) => t.id === seg[3])!;
        access.teams = access.teams.filter((t) => t.team_id !== seg[3]);
        if (method === "PUT") access.teams.push({ team_id: team.id, name: team.name, role: body.role });
        return new Response(null, { status: 204 });
      }
      if (method === "PATCH") {
        Object.assign(workspace, body);
        return workspace;
      }
      if (workspace.id === "w1") return json({ detail: "default_workspace" }, 409);
      state.workspaces = state.workspaces.filter((w) => w !== workspace);
      return new Response(null, { status: 204 });
    }
    if (seg[0] === "audit") {
      const params = new URLSearchParams(url.split("?")[1] ?? "");
      const action = params.get("action") ?? "";
      const items = state.audit.filter((e) => e.action.startsWith(action));
      const before = params.get("before");
      const start = before ? items.findIndex((e) => e.id === before) + 1 : 0;
      const page = items.slice(start, start + 2);
      return { items: page, next: start + 2 < items.length ? page.at(-1)!.id : null };
    }
    throw new Error(`unexpected ${method} ${url}`);
  };
}
