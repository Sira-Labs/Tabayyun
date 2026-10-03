import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_WORKSPACE_ID, WORKSPACE_KEY, resolveWorkspace, setWorkspaceId, type Workspace } from "../workspace";
import { ME, renderApp, stubFetch } from "./helpers";

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

const NORTH: Workspace = { id: "w-north", name: "Plant North", timezone: "UTC", created_at: "2026-10-01T00:00:00Z", role: "editor" };
const SOUTH: Workspace = { id: "w-south", name: "Plant South", timezone: "UTC", created_at: "2026-10-02T00:00:00Z", role: "viewer" };
const EMPTY = { items: [], next_cursor: null };

/** The workspace header of each `/api/runs` request, in order. */
function runsHeaders(fetch: ReturnType<typeof stubFetch>): (string | undefined)[] {
  return fetch.mock.calls
    .filter(([url]) => String(url).startsWith("/api/runs"))
    .map(([, init]) => (init?.headers as Record<string, string>)["X-Tabayyun-Workspace"]);
}

describe("Workspace picker", () => {
  it("sends the chosen workspace and switches", async () => {
    const fetch = stubFetch(() => EMPTY, { workspaces: [NORTH, SOUTH] });
    renderApp("/runs");

    const picker = await screen.findByRole("combobox", { name: "Workspace" });
    expect((picker as HTMLSelectElement).value).toBe("w-north");
    await waitFor(() => expect(runsHeaders(fetch)).toContain("w-north"));

    await userEvent.setup().selectOptions(picker, "w-south");
    await waitFor(() => expect(runsHeaders(fetch).at(-1)).toBe("w-south"));
    expect(localStorage.getItem(WORKSPACE_KEY)).toBe("w-south");
  });

  it("falls back from a stored workspace that is no longer visible", async () => {
    const fetch = stubFetch(() => EMPTY, { workspaces: [NORTH, SOUTH] });
    setWorkspaceId("w-gone");
    renderApp("/runs");

    expect(((await screen.findByRole("combobox", { name: "Workspace" })) as HTMLSelectElement).value).toBe("w-north");
    await waitFor(() => expect(runsHeaders(fetch)).toEqual(["w-north"]));
  });

  it("shows the name alone with one workspace", async () => {
    stubFetch(() => EMPTY, { workspaces: [NORTH] });
    renderApp("/runs");
    expect(await screen.findByText("Plant North")).toBeTruthy();
    expect(screen.queryByRole("combobox", { name: "Workspace" })).toBeNull();
  });

  it("explains a missing workspace", async () => {
    stubFetch(() => EMPTY, { me: { ...ME, role: "member" }, workspaces: [] });
    renderApp("/runs");
    expect(await screen.findByRole("heading", { name: "No workspace yet" })).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Admin" })).toBeNull();
  });

  it("shows Admin to org admins and workspace admins only", async () => {
    stubFetch(() => EMPTY, { me: { ...ME, role: "member" }, workspaces: [NORTH] });
    const first = renderApp("/runs");
    await screen.findByRole("heading", { name: "Runs" });
    expect(screen.queryByRole("link", { name: "Admin" })).toBeNull();
    first.unmount();

    stubFetch(() => EMPTY, { me: { ...ME, role: "member" }, workspaces: [{ ...NORTH, role: "admin" }] });
    renderApp("/runs");
    expect(await screen.findByRole("link", { name: "Admin" })).toBeTruthy();
  });

  it("lets a member without a workspace check again", async () => {
    let granted: Workspace[] = [];
    stubFetch(() => EMPTY, { me: { ...ME, role: "member" }, workspaces: () => granted });
    renderApp("/runs");
    await screen.findByRole("heading", { name: "No workspace yet" });
    granted = [NORTH];
    await userEvent.setup().click(screen.getByRole("button", { name: "Check again" }));
    expect(await screen.findByRole("heading", { name: "Runs" })).toBeTruthy();
  });
});

describe("resolveWorkspace", () => {
  const DEFAULT: Workspace = { ...SOUTH, id: DEFAULT_WORKSPACE_ID, name: "default", created_at: "2026-10-05T00:00:00Z" };

  it("keeps a visible stored choice", () => {
    expect(resolveWorkspace([NORTH, SOUTH, DEFAULT], "w-south")?.id).toBe("w-south");
  });

  it("otherwise picks what the server picks: the default workspace, else the oldest", () => {
    expect(resolveWorkspace([NORTH, SOUTH, DEFAULT], null)?.id).toBe(DEFAULT_WORKSPACE_ID);
    expect(resolveWorkspace([SOUTH, NORTH], "w-gone")?.id).toBe("w-north");
    expect(resolveWorkspace([], null)).toBeNull();
  });
});
