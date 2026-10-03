import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { adminApi, ORG_ROLES, WORKSPACE_ROLES, type Invitation, type InvitationIn, type Member, type OrgRole } from "../../admin";
import { buttonClass } from "../../components/Brand";
import { formatLocalTime } from "../../format";
import type { WorkspaceRole } from "../../workspace";
import { ErrorLine, RoleSelect, dangerButtonClass, dialogs, inputClass, linkButtonClass, panelClass, selectClass } from "./ui";

const EMAIL_STATUS: Record<Invitation["email_status"], string> = {
  not_configured: "email off",
  queued: "email queued",
  sent: "email sent",
  failed: "email failed",
};

/** Members tab: the org's name, invitations and members (org admins). */
export function Members({ myRole }: { myRole: OrgRole }) {
  return (
    <div className="space-y-6">
      <OrgName canRename={myRole === "owner"} />
      <Invite />
      <Invitations />
      <MemberList myRole={myRole} />
    </div>
  );
}

function OrgName({ canRename }: { canRename: boolean }) {
  const client = useQueryClient();
  const org = useQuery({ queryKey: ["admin", "org"], queryFn: adminApi.org });
  const [name, setName] = useState<string | null>(null);
  const rename = useMutation({
    mutationFn: (value: string) => adminApi.renameOrg(value),
    onSuccess: () => {
      setName(null);
      void client.invalidateQueries({ queryKey: ["admin", "org"] });
      void client.invalidateQueries({ queryKey: ["me"] });
    },
  });
  if (!org.data) return <ErrorLine error={org.error} prefix="Could not load the organisation" />;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (name !== null) rename.mutate(name);
  };
  return (
    <section aria-labelledby="org-heading" className={panelClass}>
      <h2 id="org-heading" className="font-semibold">
        Organisation
      </h2>
      {name === null ? (
        <p className="flex flex-wrap items-center gap-2">
          <span>{org.data.name}</span>
          {canRename && (
            <button type="button" className={linkButtonClass} onClick={() => setName(org.data.name)}>
              Rename
            </button>
          )}
        </p>
      ) : (
        <form onSubmit={submit} className="mt-2 flex flex-wrap items-center gap-2">
          <input aria-label="Organisation name" className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
          <button type="submit" className={buttonClass} disabled={rename.isPending}>
            Save
          </button>
          <button type="button" className={linkButtonClass} onClick={() => setName(null)}>
            Cancel
          </button>
        </form>
      )}
      <ErrorLine error={rename.error} prefix="Could not rename" />
    </section>
  );
}

function Invite() {
  const client = useQueryClient();
  const workspaces = useQuery({ queryKey: ["admin", "workspaces"], queryFn: adminApi.workspaces });
  const [email, setEmail] = useState("");
  const [orgRole, setOrgRole] = useState<"member" | "admin">("member");
  const [workspaceId, setWorkspaceId] = useState("");
  const [workspaceRole, setWorkspaceRole] = useState<WorkspaceRole>("viewer");
  const invite = useMutation({
    mutationFn: (body: InvitationIn) => adminApi.invite(body),
    onSuccess: () => {
      setEmail("");
      void client.invalidateQueries({ queryKey: ["admin", "invitations"] });
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    invite.mutate({
      email,
      org_role: orgRole,
      ...(workspaceId ? { workspace_id: workspaceId, workspace_role: workspaceRole } : {}),
    });
  };
  return (
    <section aria-labelledby="invite-heading" className={panelClass}>
      <h2 id="invite-heading" className="font-semibold">
        Invite someone
      </h2>
      <p className="text-sm text-slate-600 dark:text-slate-400">
        They join when they sign in with this email address, with Google, GitHub or a passkey.
      </p>
      <form onSubmit={submit} className="mt-3 flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-sm">
          Email
          <input type="email" required className={inputClass} value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Role in the organisation
          <select className={selectClass} value={orgRole} onChange={(e) => setOrgRole(e.target.value as "member" | "admin")}>
            <option value="member">member</option>
            <option value="admin">admin</option>
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Workspace
          <select className={selectClass} value={workspaceId} onChange={(e) => setWorkspaceId(e.target.value)}>
            <option value="">none</option>
            {workspaces.data?.map((w) => (
              <option key={w.id} value={w.id}>
                {w.name}
              </option>
            ))}
          </select>
        </label>
        {workspaceId && (
          <label className="flex flex-col gap-1 text-sm">
            Role in the workspace
            <RoleSelect label="Role in the workspace" value={workspaceRole} roles={WORKSPACE_ROLES} onChange={setWorkspaceRole} />
          </label>
        )}
        <button type="submit" className={buttonClass} disabled={invite.isPending}>
          Invite
        </button>
      </form>
      {invite.isSuccess && (
        <p role="status" className="mt-2 text-sm text-emerald-800 dark:text-emerald-300">
          Invited {invite.data.email}
          {invite.data.email_status === "not_configured" ? " (email is off on this server: tell them to sign in)." : "."}
        </p>
      )}
      <ErrorLine error={invite.error} prefix="Could not invite" />
    </section>
  );
}

function Invitations() {
  const client = useQueryClient();
  const invitations = useQuery({ queryKey: ["admin", "invitations"], queryFn: () => adminApi.invitations("all") });
  const refresh = () => client.invalidateQueries({ queryKey: ["admin", "invitations"] });
  const resend = useMutation({ mutationFn: adminApi.resendInvitation, onSuccess: refresh });
  const revoke = useMutation({ mutationFn: adminApi.revokeInvitation, onSuccess: refresh });
  // Open ones first, then the rest by date (the server sends newest first).
  const items = [...(invitations.data ?? [])].sort((a, b) => Number(b.status === "pending") - Number(a.status === "pending"));
  return (
    <section aria-labelledby="invitations-heading" className="space-y-2">
      <h2 id="invitations-heading" className="font-semibold">
        Invitations
      </h2>
      <ErrorLine error={invitations.error} prefix="Could not load invitations" />
      <ErrorLine error={resend.error ?? revoke.error} prefix="Could not change the invitation" />
      {invitations.isSuccess && items.length === 0 && <p className="text-slate-600 dark:text-slate-400">No invitations yet.</p>}
      {items.length > 0 && (
        <ul className="divide-y divide-slate-200 rounded-lg border border-slate-200 bg-white dark:divide-slate-800 dark:border-slate-800 dark:bg-slate-900">
          {items.map((i) => (
            <li key={i.id} aria-label={i.email} className="flex flex-wrap items-center justify-between gap-2 p-3 text-sm">
              <div>
                <div className="font-medium">{i.email}</div>
                <div className="text-xs text-slate-600 dark:text-slate-400">
                  {i.org_role}
                  {i.workspace && ` · ${i.workspace_role} of ${i.workspace.name}`} · {i.status}
                  {i.status === "pending" && ` until ${formatLocalTime(i.expires_at)}`} · {EMAIL_STATUS[i.email_status]}
                  {i.email_error && ` (${i.email_error})`}
                </div>
              </div>
              <div className="flex gap-1">
                {(i.status === "pending" || i.status === "expired") && (
                  <button type="button" className={linkButtonClass} disabled={resend.isPending} onClick={() => resend.mutate(i.id)}>
                    Resend
                  </button>
                )}
                {i.status === "pending" && (
                  <button
                    type="button"
                    className={dangerButtonClass}
                    disabled={revoke.isPending}
                    onClick={() => dialogs.confirm(`Revoke the invitation for ${i.email}?`) && revoke.mutate(i.id)}
                  >
                    Revoke
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function MemberList({ myRole }: { myRole: OrgRole }) {
  const client = useQueryClient();
  const [search, setSearch] = useState("");
  const members = useInfiniteQuery({
    queryKey: ["admin", "members", search],
    queryFn: ({ pageParam }) => adminApi.members({ q: search || undefined, cursor: pageParam }),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next,
  });
  const refresh = () => client.invalidateQueries({ queryKey: ["admin", "members"] });
  const setRole = useMutation({
    mutationFn: ({ member, role }: { member: Member; role: OrgRole }) => adminApi.setMemberRole(member.user_id, role),
    onSuccess: refresh,
  });
  const remove = useMutation({ mutationFn: (member: Member) => adminApi.removeMember(member.user_id), onSuccess: refresh });
  const items = members.data?.pages.flatMap((p) => p.items) ?? [];
  // Admins may not grant owner or change an owner; the server enforces it too.
  const rolesFor = (member: Member): OrgRole[] =>
    myRole === "owner" ? ORG_ROLES : member.role === "owner" ? ["owner"] : ["admin", "member"];

  return (
    <section aria-labelledby="members-heading" className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="members-heading" className="font-semibold">
          Members
        </h2>
        <input
          type="search"
          aria-label="Search members"
          placeholder="Search by email or name"
          className={inputClass}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>
      <ErrorLine error={members.error} prefix="Could not load members" />
      <ErrorLine error={setRole.error ?? remove.error} prefix="Could not change the member" />
      {items.length > 0 && (
        <ul className="divide-y divide-slate-200 rounded-lg border border-slate-200 bg-white dark:divide-slate-800 dark:border-slate-800 dark:bg-slate-900">
          {items.map((m) => (
            <li key={m.user_id} aria-label={m.email} className="flex flex-wrap items-center justify-between gap-2 p-3 text-sm">
              <div>
                <div className="font-medium">{m.display_name}</div>
                <div className="text-xs text-slate-600 dark:text-slate-400">
                  {m.email}
                  {m.disabled && " · disabled"}
                </div>
              </div>
              <div className="flex items-center gap-2">
                <RoleSelect
                  label={`Role of ${m.email}`}
                  value={m.role}
                  roles={rolesFor(m)}
                  disabled={setRole.isPending || (myRole !== "owner" && m.role === "owner")}
                  onChange={(role) => setRole.mutate({ member: m, role })}
                />
                <button
                  type="button"
                  className={dangerButtonClass}
                  disabled={remove.isPending || (myRole !== "owner" && m.role === "owner")}
                  onClick={() => dialogs.confirm(`Remove ${m.email} from the organisation?`) && remove.mutate(m)}
                >
                  Remove
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
      {members.isSuccess && items.length === 0 && <p className="text-slate-600 dark:text-slate-400">No members match.</p>}
      {members.hasNextPage && (
        <button type="button" className={buttonClass} disabled={members.isFetchingNextPage} onClick={() => void members.fetchNextPage()}>
          Load more
        </button>
      )}
    </section>
  );
}
