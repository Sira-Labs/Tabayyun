import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { adminApi, type Team } from "../../admin";
import { buttonClass } from "../../components/Brand";
import { ErrorLine, dangerButtonClass, dialogs, inputClass, linkButtonClass, panelClass, selectClass } from "./ui";

/** Teams tab (org admins): create, rename and delete teams and choose their members. A team's
 * roles in workspaces are given in each workspace's access list. */
export function Teams() {
  const client = useQueryClient();
  const teams = useQuery({ queryKey: ["admin", "teams"], queryFn: adminApi.teams });
  const [name, setName] = useState("");
  const create = useMutation({
    mutationFn: () => adminApi.createTeam(name),
    onSuccess: () => {
      setName("");
      void client.invalidateQueries({ queryKey: ["admin", "teams"] });
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    create.mutate();
  };
  return (
    <div className="space-y-6">
      <section aria-labelledby="create-team-heading" className={panelClass}>
        <h2 id="create-team-heading" className="font-semibold">
          New team
        </h2>
        <form onSubmit={submit} className="mt-3 flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-sm">
            Name
            <input required className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <button type="submit" className={buttonClass} disabled={create.isPending}>
            Create
          </button>
        </form>
        <ErrorLine error={create.error} prefix="Could not create the team" />
      </section>
      <ErrorLine error={teams.error} prefix="Could not load teams" />
      {teams.isSuccess && teams.data.length === 0 && <p className="text-slate-600 dark:text-slate-400">No teams yet.</p>}
      {teams.data?.map((t) => <TeamCard key={t.id} team={t} />)}
    </div>
  );
}

function TeamCard({ team }: { team: Team }) {
  const client = useQueryClient();
  const members = useQuery({ queryKey: ["admin", "members", "pick"], queryFn: () => adminApi.members({ limit: 200 }) });
  const refresh = () => client.invalidateQueries({ queryKey: ["admin", "teams"] });
  const [renaming, setRenaming] = useState<string | null>(null);
  const [newMember, setNewMember] = useState("");
  const rename = useMutation({
    mutationFn: (value: string) => adminApi.renameTeam(team.id, value),
    onSuccess: () => {
      setRenaming(null);
      void refresh();
    },
  });
  const remove = useMutation({ mutationFn: () => adminApi.deleteTeam(team.id), onSuccess: refresh });
  const addMember = useMutation({ mutationFn: (userId: string) => adminApi.addTeamMember(team.id, userId), onSuccess: refresh });
  const removeMember = useMutation({
    mutationFn: (userId: string) => adminApi.removeTeamMember(team.id, userId),
    onSuccess: refresh,
  });
  const inTeam = new Set(team.members.map((m) => m.user_id));
  const candidates = (members.data?.items ?? []).filter((m) => !inTeam.has(m.user_id));
  const headingId = `team-${team.id}`;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (renaming !== null) rename.mutate(renaming);
  };

  return (
    <section aria-labelledby={headingId} className={panelClass}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id={headingId} className="font-semibold">
          {team.name}
        </h2>
        <div className="flex gap-1">
          <button type="button" className={linkButtonClass} onClick={() => setRenaming(team.name)}>
            Rename
          </button>
          <button
            type="button"
            className={dangerButtonClass}
            disabled={remove.isPending}
            onClick={() => dialogs.confirm(`Delete the team ${team.name}? Its workspace roles go with it.`) && remove.mutate()}
          >
            Delete
          </button>
        </div>
      </div>
      {renaming !== null && (
        <form onSubmit={submit} className="mt-2 flex flex-wrap items-center gap-2">
          <input aria-label="Team name" className={inputClass} value={renaming} onChange={(e) => setRenaming(e.target.value)} />
          <button type="submit" className={buttonClass} disabled={rename.isPending}>
            Save
          </button>
        </form>
      )}
      <ErrorLine error={rename.error ?? remove.error ?? addMember.error ?? removeMember.error} prefix="Could not change the team" />
      <p className="mt-2 text-sm text-slate-600 dark:text-slate-400">
        {team.workspaces.length === 0
          ? "No role in any workspace yet."
          : `Roles: ${team.workspaces.map((w) => `${w.role} of ${w.name}`).join(", ")}.`}
      </p>
      <ul aria-label={`Members of ${team.name}`} className="mt-2 flex flex-wrap gap-2 text-sm">
        {team.members.map((m) => (
          <li key={m.user_id} className="flex items-center gap-1 rounded bg-slate-100 px-2 py-1 dark:bg-slate-800">
            {m.email}
            <button
              type="button"
              aria-label={`Remove ${m.email} from ${team.name}`}
              className={dangerButtonClass}
              onClick={() => removeMember.mutate(m.user_id)}
            >
              ×
            </button>
          </li>
        ))}
      </ul>
      {candidates.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <select aria-label={`Person to add to ${team.name}`} className={selectClass} value={newMember} onChange={(e) => setNewMember(e.target.value)}>
            <option value="">Add a person…</option>
            {candidates.map((m) => (
              <option key={m.user_id} value={m.user_id}>
                {m.email}
              </option>
            ))}
          </select>
          <button
            type="button"
            className={buttonClass}
            disabled={!newMember || addMember.isPending}
            onClick={() => {
              addMember.mutate(newMember);
              setNewMember("");
            }}
          >
            Add to team
          </button>
        </div>
      )}
    </section>
  );
}
