import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { DEFAULT_WORKSPACE_ID, adminApi, WORKSPACE_ROLES, type AdminWorkspace } from "../../admin";
import { buttonClass } from "../../components/Brand";
import type { WorkspaceRole } from "../../workspace";
import { ErrorLine, RoleSelect, dangerButtonClass, dialogs, inputClass, linkButtonClass, panelClass, selectClass } from "./ui";

/** The browser's time zone, the default for a new workspace. */
function localZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/** Workspaces tab: the workspaces the caller administers, each with its access list; org
 * admins create them, owners delete empty ones. */
export function Workspaces({ isOrgAdmin, isOwner }: { isOrgAdmin: boolean; isOwner: boolean }) {
  const workspaces = useQuery({ queryKey: ["admin", "workspaces"], queryFn: adminApi.workspaces });
  return (
    <div className="space-y-6">
      {isOrgAdmin && <CreateWorkspace />}
      <ErrorLine error={workspaces.error} prefix="Could not load workspaces" />
      {workspaces.data?.map((w) => <WorkspaceCard key={w.id} workspace={w} canDelete={isOwner} />)}
    </div>
  );
}

function useRefreshWorkspaces() {
  const client = useQueryClient();
  return () => {
    void client.invalidateQueries({ queryKey: ["admin", "workspaces"] });
    // The header's picker lists workspaces too.
    void client.invalidateQueries({ queryKey: ["workspaces"] });
  };
}

function CreateWorkspace() {
  const refresh = useRefreshWorkspaces();
  const [name, setName] = useState("");
  const [timezone, setTimezone] = useState(localZone);
  const create = useMutation({
    mutationFn: () => adminApi.createWorkspace(name, timezone),
    onSuccess: () => {
      setName("");
      refresh();
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    create.mutate();
  };
  return (
    <section aria-labelledby="create-workspace-heading" className={panelClass}>
      <h2 id="create-workspace-heading" className="font-semibold">
        New workspace
      </h2>
      <form onSubmit={submit} className="mt-3 flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-sm">
          Name
          <input required className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Time zone
          <input required className={inputClass} value={timezone} onChange={(e) => setTimezone(e.target.value)} />
        </label>
        <button type="submit" className={buttonClass} disabled={create.isPending}>
          Create
        </button>
      </form>
      <ErrorLine error={create.error} prefix="Could not create the workspace" />
    </section>
  );
}

function WorkspaceCard({ workspace, canDelete }: { workspace: AdminWorkspace; canDelete: boolean }) {
  const refresh = useRefreshWorkspaces();
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(workspace.name);
  const [timezone, setTimezone] = useState(workspace.timezone);
  const [showAccess, setShowAccess] = useState(false);
  const update = useMutation({
    mutationFn: () => adminApi.updateWorkspace(workspace.id, { name, timezone }),
    onSuccess: () => {
      setEditing(false);
      refresh();
    },
  });
  const remove = useMutation({ mutationFn: () => adminApi.deleteWorkspace(workspace.id), onSuccess: refresh });
  const headingId = `ws-${workspace.id}`;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    update.mutate();
  };

  return (
    <section aria-labelledby={headingId} className={panelClass}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id={headingId} className="font-semibold">
          {workspace.name} <span className="text-sm font-normal text-slate-600 dark:text-slate-400">· {workspace.timezone}</span>
        </h2>
        <div className="flex gap-1">
          <button type="button" className={linkButtonClass} aria-expanded={showAccess} onClick={() => setShowAccess((v) => !v)}>
            {showAccess ? "Hide access" : "Manage access"}
          </button>
          <button type="button" className={linkButtonClass} onClick={() => setEditing((v) => !v)}>
            Edit
          </button>
          {canDelete && workspace.id !== DEFAULT_WORKSPACE_ID && (
            <button
              type="button"
              className={dangerButtonClass}
              disabled={remove.isPending}
              onClick={() => dialogs.confirm(`Delete the workspace ${workspace.name}?`) && remove.mutate()}
            >
              Delete
            </button>
          )}
        </div>
      </div>
      {editing && (
        <form onSubmit={submit} className="mt-3 flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-sm">
            Name
            <input required className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            Time zone
            <input required className={inputClass} value={timezone} onChange={(e) => setTimezone(e.target.value)} />
          </label>
          <button type="submit" className={buttonClass} disabled={update.isPending}>
            Save
          </button>
        </form>
      )}
      <ErrorLine error={update.error} prefix="Could not save" />
      <ErrorLine error={remove.error} prefix="Could not delete" />
      {showAccess && <AccessPanel workspace={workspace} />}
    </section>
  );
}

function AccessPanel({ workspace }: { workspace: AdminWorkspace }) {
  const client = useQueryClient();
  const key = ["admin", "access", workspace.id];
  const access = useQuery({ queryKey: key, queryFn: () => adminApi.access(workspace.id) });
  const members = useQuery({ queryKey: ["admin", "members", "pick"], queryFn: () => adminApi.members({ limit: 200 }) });
  const teams = useQuery({ queryKey: ["admin", "teams"], queryFn: adminApi.teams });
  const refresh = () => client.invalidateQueries({ queryKey: key });
  const setMember = useMutation({
    mutationFn: ({ userId, role }: { userId: string; role: WorkspaceRole }) => adminApi.setWorkspaceMember(workspace.id, userId, role),
    onSuccess: refresh,
  });
  const removeMember = useMutation({
    mutationFn: (userId: string) => adminApi.removeWorkspaceMember(workspace.id, userId),
    onSuccess: refresh,
  });
  const setTeam = useMutation({
    mutationFn: ({ teamId, role }: { teamId: string; role: WorkspaceRole }) => adminApi.setWorkspaceTeam(workspace.id, teamId, role),
    onSuccess: refresh,
  });
  const removeTeam = useMutation({ mutationFn: (teamId: string) => adminApi.removeWorkspaceTeam(workspace.id, teamId), onSuccess: refresh });
  const [newMember, setNewMember] = useState("");
  const [newTeam, setNewTeam] = useState("");
  const [memberRole, setMemberRole] = useState<WorkspaceRole>("viewer");
  const [teamRole, setTeamRole] = useState<WorkspaceRole>("viewer");
  const error = setMember.error ?? removeMember.error ?? setTeam.error ?? removeTeam.error;

  if (!access.data) return <ErrorLine error={access.error} prefix="Could not load access" />;
  const granted = new Set(access.data.members.map((m) => m.user_id));
  const candidates = (members.data?.items ?? []).filter((m) => !granted.has(m.user_id));
  const teamGranted = new Set(access.data.teams.map((t) => t.team_id));
  const teamCandidates = (teams.data ?? []).filter((t) => !teamGranted.has(t.id));

  return (
    <div className="mt-4 space-y-4 text-sm">
      <ErrorLine error={error} prefix="Could not change access" />
      <section aria-label={`People in ${workspace.name}`} className="space-y-2">
        <h3 className="font-medium">People</h3>
        {access.data.members.length === 0 && access.data.org_admins.length === 0 && (
          <p className="text-slate-600 dark:text-slate-400">No one has a direct role here yet.</p>
        )}
        <ul className="space-y-1">
          {access.data.members.map((m) => (
            <li key={m.user_id} aria-label={m.email} className="flex flex-wrap items-center gap-2">
              <span className="min-w-48">{m.email}</span>
              <RoleSelect
                label={`Role of ${m.email} in ${workspace.name}`}
                value={m.role}
                roles={WORKSPACE_ROLES}
                onChange={(role) => setMember.mutate({ userId: m.user_id, role })}
              />
              <button type="button" className={dangerButtonClass} onClick={() => removeMember.mutate(m.user_id)}>
                Remove
              </button>
            </li>
          ))}
          {access.data.org_admins.map((a) => (
            <li key={`admin-${a.user_id}`} className="flex flex-wrap items-center gap-2 text-slate-600 dark:text-slate-400">
              <span className="min-w-48">{a.email}</span>
              <span>admin (org {a.role})</span>
            </li>
          ))}
        </ul>
        {candidates.length > 0 && (
          <div className="flex flex-wrap items-center gap-2">
            <select aria-label="Person to add" className={selectClass} value={newMember} onChange={(e) => setNewMember(e.target.value)}>
              <option value="">Add a person…</option>
              {candidates.map((m) => (
                <option key={m.user_id} value={m.user_id}>
                  {m.email}
                </option>
              ))}
            </select>
            <RoleSelect label="Role for the person" value={memberRole} roles={WORKSPACE_ROLES} onChange={setMemberRole} />
            <button
              type="button"
              className={buttonClass}
              disabled={!newMember || setMember.isPending}
              onClick={() => {
                setMember.mutate({ userId: newMember, role: memberRole });
                setNewMember("");
              }}
            >
              Add person
            </button>
          </div>
        )}
      </section>
      <section aria-label={`Teams in ${workspace.name}`} className="space-y-2">
        <h3 className="font-medium">Teams</h3>
        {access.data.teams.length === 0 && <p className="text-slate-600 dark:text-slate-400">No team has a role here.</p>}
        <ul className="space-y-1">
          {access.data.teams.map((t) => (
            <li key={t.team_id} aria-label={t.name} className="flex flex-wrap items-center gap-2">
              <span className="min-w-48">{t.name}</span>
              <RoleSelect
                label={`Role of team ${t.name} in ${workspace.name}`}
                value={t.role}
                roles={WORKSPACE_ROLES}
                onChange={(role) => setTeam.mutate({ teamId: t.team_id, role })}
              />
              <button type="button" className={dangerButtonClass} onClick={() => removeTeam.mutate(t.team_id)}>
                Remove
              </button>
            </li>
          ))}
        </ul>
        {teamCandidates.length > 0 && (
          <div className="flex flex-wrap items-center gap-2">
            <select aria-label="Team to add" className={selectClass} value={newTeam} onChange={(e) => setNewTeam(e.target.value)}>
              <option value="">Add a team…</option>
              {teamCandidates.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
            <RoleSelect label="Role for the team" value={teamRole} roles={WORKSPACE_ROLES} onChange={setTeamRole} />
            <button
              type="button"
              className={buttonClass}
              disabled={!newTeam || setTeam.isPending}
              onClick={() => {
                setTeam.mutate({ teamId: newTeam, role: teamRole });
                setNewTeam("");
              }}
            >
              Add team
            </button>
          </div>
        )}
      </section>
    </div>
  );
}
