import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { dialogs } from "../pages/admin/ui";
import { adminRoute, adminState } from "./adminFake";
import { ME, renderApp, stubFetch } from "./helpers";

afterEach(() => vi.restoreAllMocks());

describe("Admin: workspaces", () => {
  it("creates a workspace and gives a person and a team a role in it", async () => {
    const state = adminState();
    stubFetch(adminRoute(state));
    const user = userEvent.setup();
    renderApp("/admin?tab=workspaces");

    const name = await screen.findByRole("textbox", { name: "Name" });
    await user.type(name, "Plant North");
    const zone = screen.getByRole("textbox", { name: "Time zone" });
    await user.clear(zone);
    await user.type(zone, "Europe/Berlin");
    await user.click(screen.getByRole("button", { name: "Create" }));
    const card = await screen.findByRole("region", { name: /Plant North/ });
    expect(state.requests.at(-1)?.body).toEqual({ name: "Plant North", timezone: "Europe/Berlin" });

    await user.click(within(card).getByRole("button", { name: "Manage access" }));
    await user.selectOptions(await within(card).findByRole("combobox", { name: "Person to add" }), "u2");
    await user.selectOptions(within(card).getByRole("combobox", { name: "Role for the person" }), "editor");
    await user.click(within(card).getByRole("button", { name: "Add person" }));
    expect(await within(card).findByRole("listitem", { name: "mia@example.org" })).toBeTruthy();
    expect(state.requests.at(-1)).toEqual({ method: "PUT", url: "/api/admin/workspaces/w2/members/u2", body: { role: "editor" } });

    await user.selectOptions(within(card).getByRole("combobox", { name: "Team to add" }), "t1");
    await user.click(within(card).getByRole("button", { name: "Add team" }));
    expect(await within(card).findByRole("listitem", { name: "ops" })).toBeTruthy();
    expect(state.requests.at(-1)).toEqual({ method: "PUT", url: "/api/admin/workspaces/w2/teams/t1", body: { role: "viewer" } });
  });

  it("explains why the default workspace cannot be deleted", async () => {
    stubFetch(adminRoute(adminState()));
    vi.spyOn(dialogs, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    renderApp("/admin?tab=workspaces");
    const card = await screen.findByRole("region", { name: /default/ });
    await user.click(within(card).getByRole("button", { name: "Delete" }));
    expect((await within(card).findByRole("alert")).textContent).toContain("default workspace cannot be deleted");
  });

  it("gives workspace admins only their workspaces and the log", async () => {
    stubFetch(adminRoute(adminState()), { me: { ...ME, role: "member" }, workspaces: [{ id: "w1", name: "default", timezone: "UTC", role: "admin" }] });
    renderApp("/admin");
    const nav = await screen.findByRole("navigation", { name: "Admin sections" });
    expect(within(nav).getAllByRole("link").map((a) => a.textContent)).toEqual(["Workspaces", "Audit log"]);
    expect(await screen.findByRole("region", { name: /default/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Create" })).toBeNull();
    await waitFor(() => expect(screen.queryByRole("button", { name: "Delete" })).toBeNull());
  });
});
