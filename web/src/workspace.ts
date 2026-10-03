// The workspace the app works in (spec 014). The choice lives in localStorage and travels as
// `X-Tabayyun-Workspace` on every API call; the server falls back to a visible default when the
// header is missing. Storage can be unavailable (private mode, blocked site data), so every
// access is guarded and the in-memory value is what requests read.
export type WorkspaceRole = "admin" | "editor" | "viewer";
export type Workspace = { id: string; name: string; timezone: string; created_at: string; role: WorkspaceRole };

export const WORKSPACE_KEY = "tby.workspace";
export const WORKSPACE_HEADER = "X-Tabayyun-Workspace";
/** The workspace every install starts with (spec 001). */
export const DEFAULT_WORKSPACE_ID = "00000000-0000-0000-0000-000000000002";

let current: string | null = readStored();

function readStored(): string | null {
  try {
    return globalThis.localStorage?.getItem(WORKSPACE_KEY) ?? null;
  } catch {
    return null;
  }
}

/** The workspace id requests carry, or null to let the server choose. */
export function currentWorkspaceId(): string | null {
  return current;
}

/** Remember the chosen workspace for this browser. */
export function setWorkspaceId(id: string | null): void {
  current = id;
  try {
    if (id) globalThis.localStorage?.setItem(WORKSPACE_KEY, id);
    else globalThis.localStorage?.removeItem(WORKSPACE_KEY);
  } catch {
    // Storage unavailable: the choice lasts until the page reloads.
  }
}

/** The workspace to use from the visible ones: the stored choice while it is visible, else what
 * the server picks without a header (`get_workspace_id`): the default workspace, else the oldest. */
export function resolveWorkspace(workspaces: Workspace[], stored: string | null): Workspace | null {
  const chosen = workspaces.find((w) => w.id === stored) ?? workspaces.find((w) => w.id === DEFAULT_WORKSPACE_ID);
  if (chosen) return chosen;
  const byAge = [...workspaces].sort((a, b) => a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id));
  return byAge[0] ?? null;
}
