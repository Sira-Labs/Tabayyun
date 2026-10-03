import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WORKSPACE_KEY, setWorkspaceId, type Workspace } from "../workspace";
import { ME, renderApp, stubFetch } from "./helpers";

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

const NORTH: Workspace = { id: "w-north", name: "Plant North", timezone: "UTC", role: "editor" };
const SOUTH: Workspace = { id: "w-south", name: "Plant South", timezone: "UTC", role: "viewer" };
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
});
