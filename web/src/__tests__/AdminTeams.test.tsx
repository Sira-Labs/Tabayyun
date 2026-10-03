import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { dialogs } from "../pages/admin/ui";
import { adminRoute, adminState } from "./adminFake";
import { renderApp, stubFetch } from "./helpers";

afterEach(() => vi.restoreAllMocks());

describe("Admin: teams", () => {
  it("creates a team, adds and removes a member, and deletes it", async () => {
    const state = adminState();
    stubFetch(adminRoute(state));
    const user = userEvent.setup();
    renderApp("/admin?tab=teams");

    await user.type(await screen.findByRole("textbox", { name: "Name" }), "Night shift");
    await user.click(screen.getByRole("button", { name: "Create" }));
    const card = await screen.findByRole("region", { name: "Night shift" });

    await user.selectOptions(within(card).getByRole("combobox", { name: "Person to add to Night shift" }), "u2");
    await user.click(within(card).getByRole("button", { name: "Add to team" }));
    const members = await within(card).findByRole("list", { name: "Members of Night shift" });
    await waitFor(() => expect(members.textContent).toContain("mia@example.org"));

    await user.click(within(card).getByRole("button", { name: "Remove mia@example.org from Night shift" }));
    await waitFor(() => expect(members.textContent).not.toContain("mia@example.org"));

    vi.spyOn(dialogs, "confirm").mockReturnValue(true);
    await user.click(within(card).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Night shift" })).toBeNull());
  });
});
