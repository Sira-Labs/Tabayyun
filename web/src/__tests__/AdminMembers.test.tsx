import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { dialogs } from "../pages/admin/ui";
import { adminRoute, adminState } from "./adminFake";
import { renderApp, stubFetch } from "./helpers";

afterEach(() => vi.restoreAllMocks());

describe("Admin: members and invitations", () => {
  it("invites with a workspace role and lists the invitation", async () => {
    const state = adminState();
    stubFetch(adminRoute(state));
    const user = userEvent.setup();
    renderApp("/admin");

    await user.type(await screen.findByLabelText("Email"), "ada@example.org");
    await user.selectOptions(screen.getByLabelText("Workspace"), "w1");
    await user.selectOptions(screen.getByRole("combobox", { name: "Role in the workspace" }), "editor");
    await user.click(screen.getByRole("button", { name: "Invite" }));

    expect((await screen.findByRole("status")).textContent).toContain("Invited ada@example.org");
    const item = await screen.findByRole("listitem", { name: "ada@example.org" });
    expect(item.textContent).toContain("editor of default");
    expect(item.textContent).toContain("email queued");
    expect(state.requests.at(-1)).toEqual({
      method: "POST",
      url: "/api/admin/invitations",
      body: { email: "ada@example.org", org_role: "member", workspace_id: "w1", workspace_role: "editor" },
    });
  });

  it("resends and revokes an invitation after confirming", async () => {
    const state = adminState();
    stubFetch(adminRoute(state));
    const user = userEvent.setup();
    renderApp("/admin");
    await user.type(await screen.findByLabelText("Email"), "ada@example.org");
    await user.click(screen.getByRole("button", { name: "Invite" }));
    const item = await screen.findByRole("listitem", { name: "ada@example.org" });

    await user.click(within(item).getByRole("button", { name: "Resend" }));
    await waitFor(() => expect(state.requests.at(-1)?.url).toBe("/api/admin/invitations/i1/resend"));

    const confirm = vi.spyOn(dialogs, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    await user.click(within(item).getByRole("button", { name: "Revoke" }));
    expect(state.requests.at(-1)?.method).toBe("POST"); // declined: nothing sent
    await user.click(within(item).getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(state.requests.at(-1)).toMatchObject({ method: "DELETE", url: "/api/admin/invitations/i1" }));
    expect(confirm).toHaveBeenCalledWith("Revoke the invitation for ada@example.org?");
  });

  it("changes a role and removes a member", async () => {
    const state = adminState();
    stubFetch(adminRoute(state));
    const user = userEvent.setup();
    renderApp("/admin?tab=members");

    await user.selectOptions(await screen.findByRole("combobox", { name: "Role of mia@example.org" }), "admin");
    await waitFor(() => expect(state.requests.at(-1)).toEqual({ method: "PATCH", url: "/api/admin/members/u2", body: { role: "admin" } }));

    vi.spyOn(dialogs, "confirm").mockReturnValue(true);
    const mia = screen.getByRole("listitem", { name: "mia@example.org" });
    await user.click(within(mia).getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(screen.queryByRole("listitem", { name: "mia@example.org" })).toBeNull());
  });

  it("shows the server's reason in words", async () => {
    const state = adminState();
    const route = adminRoute(state);
    stubFetch((url, init) =>
      url === "/api/admin/members/u1" && init?.method === "PATCH"
        ? new Response(JSON.stringify({ detail: "last_owner" }), { status: 409 })
        : route(url, init),
    );
    const user = userEvent.setup();
    renderApp("/admin");
    await user.selectOptions(await screen.findByRole("combobox", { name: "Role of ana@example.org" }), "member");
    expect((await screen.findByRole("alert")).textContent).toContain("needs at least one owner");
  });
});
