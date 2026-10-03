import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { setWorkspaceId, type Workspace } from "../workspace";

// Queries that do not depend on the workspace survive a switch.
const KEEP = new Set(["me", "workspaces", "version", "sessions"]);

/** The current workspace in the header; a menu that switches when there are several (spec 014).
 * Switching resets every workspace-bound query and opens the runs list. */
export function WorkspacePicker({ workspaces, current }: { workspaces: Workspace[]; current: Workspace }) {
  const client = useQueryClient();
  const navigate = useNavigate();

  if (workspaces.length < 2) {
    return <span className="text-xs text-slate-600 dark:text-slate-400">{current.name}</span>;
  }

  const choose = (id: string) => {
    if (id === current.id) return;
    setWorkspaceId(id);
    // Reset (not just remove) so the queries on screen refetch in the new workspace.
    void client.resetQueries({ predicate: (q) => !KEEP.has(String(q.queryKey[0])) });
    // The list itself re-renders the layout with the new choice.
    void client.invalidateQueries({ queryKey: ["workspaces"] });
    void navigate({ to: "/runs" });
  };

  return (
    <label className="flex items-center gap-2 text-xs text-slate-600 dark:text-slate-400">
      Workspace
      <select
        value={current.id}
        onChange={(e) => choose(e.target.value)}
        className="min-h-9 rounded border border-slate-300 bg-white px-2 text-sm text-slate-900 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
      >
        {workspaces.map((w) => (
          <option key={w.id} value={w.id}>
            {w.name}
          </option>
        ))}
      </select>
    </label>
  );
}
