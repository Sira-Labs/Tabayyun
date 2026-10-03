import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AuditEvent } from "../admin";
import { describeDetails } from "../pages/admin/Audit";
import { adminRoute, adminState } from "./adminFake";
import { renderApp, stubFetch } from "./helpers";

afterEach(() => vi.restoreAllMocks());

function event(id: string, action: string, details: Record<string, unknown>): AuditEvent {
  return {
    id,
    created_at: "2026-10-03T10:00:00Z",
    actor: { user_id: "u1", email: "ana@example.org", display_name: "Ana" },
    action,
    target_type: "user",
    target_id: "u2",
    workspace_id: null,
    details,
    ip_address: "203.0.113.9",
  };
}

describe("Admin: audit log", () => {
  it("filters by area and loads more", async () => {
    const state = adminState();
    state.audit = [
      event("e3", "member.role_changed", { email: "mia@example.org", before: "member", after: "admin" }),
      event("e2", "team.created", { name: "ops" }),
      event("e1", "member.removed", { email: "bo@example.org", role: "member" }),
    ];
    stubFetch(adminRoute(state));
    const user = userEvent.setup();
    renderApp("/admin?tab=audit");

    const table = await screen.findByRole("table");
    expect(within(table).getAllByRole("row")).toHaveLength(3); // header + a page of two
    expect(table.textContent).toContain("email: mia@example.org · member → admin");
    await user.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(within(screen.getByRole("table")).getAllByRole("row")).toHaveLength(4));

    await user.selectOptions(screen.getByRole("combobox", { name: "Area" }), "team.");
    await waitFor(() => expect(within(screen.getByRole("table")).getAllByRole("row")).toHaveLength(2));
    expect(screen.getByRole("table").textContent).toContain("team.created");
  });

  it("describes nested changes", () => {
    expect(describeDetails({ timezone: { before: "UTC", after: "Asia/Riyadh" } })).toBe("timezone: UTC → Asia/Riyadh");
    expect(describeDetails({ workspace: "North", email: "a@x.org", before: null, after: "editor" })).toBe(
      "workspace: North · email: a@x.org · none → editor",
    );
  });
});
