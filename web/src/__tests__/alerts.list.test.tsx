import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { renderApp, stubFetch } from "./helpers";
import { HOOK_ID, RULE_ID, alertsBackend, emailRule, hookRule, notification } from "./alertsFake";
import { asRole } from "./sourcesFake";

const cells = (row: HTMLElement) => within(row).getAllByRole("cell").map((c) => c.textContent);

describe("alerts.list", () => {
  it("lists the rules with what they watch, target, throttle, state and last notification", async () => {
    const failed = notification({ status: "failed", sent_at: null, due_at: "2026-10-09T09:00:00Z", attempts: 3, error: "connection refused" });
    const rules = [emailRule(), hookRule({ enabled: false, last_notification: failed })];
    stubFetch(alertsBackend({ rules }).route);
    renderApp("/alerts");
    const table = await screen.findByRole("table", { name: "Alert rules by name" });
    const rows = within(table).getAllByRole("row").slice(1) as HTMLElement[];
    expect(cells(rows[0] as HTMLElement)).toEqual([
      "Critical on feeders",
      "New findings of severity critical or higher on plant/feeder…",
      "email: ops@example.org",
      "1 hour",
      "enabled",
      "sent2026-10-09 11:01:05 UTC+03:00",
    ]);
    expect(cells(rows[1] as HTMLElement)).toEqual([
      "Score drop",
      "Series whose overall score falls below 70",
      "webhook: hooks.example.com",
      "none",
      "disabled",
      "failed2026-10-09 12:00:00 UTC+03:00connection refused",
    ]);
    expect(within(rows[0] as HTMLElement).getByRole("link", { name: "Critical on feeders" }).getAttribute("href")).toBe(`/alerts/${RULE_ID}`);
    expect(within(rows[1] as HTMLElement).getByRole("link", { name: "Score drop" }).getAttribute("href")).toBe(`/alerts/${HOOK_ID}`);
    expect(screen.getByRole("link", { name: "New rule" }).getAttribute("href")).toBe("/alerts/new");
    expect(within(screen.getByRole("navigation", { name: "Main" })).getByRole("link", { name: "Alerts" }).getAttribute("href")).toBe("/alerts");
  });

  it("shows a dash for a rule that never notified, and an empty state", async () => {
    const first = renderAppWith([emailRule({ last_notification: null })]);
    const table = await screen.findByRole("table", { name: "Alert rules by name" });
    expect(cells(within(table).getAllByRole("row")[1] as HTMLElement).at(-1)).toBe("—");
    first.unmount();
    renderAppWith([]);
    expect(await screen.findByText(/No alert rules yet/)).toBeTruthy();
  });

  it("offers New rule to workspace admins only", async () => {
    for (const role of ["viewer", "editor"] as const) {
      stubFetch(alertsBackend().route, asRole(role));
      const view = renderApp("/alerts");
      await screen.findByRole("table");
      expect(screen.queryByRole("link", { name: "New rule" })).toBeNull();
      view.unmount();
    }
    stubFetch(alertsBackend().route, asRole("admin"));
    renderApp("/alerts");
    expect(await screen.findByRole("link", { name: "New rule" })).toBeTruthy();
  });
});

function renderAppWith(rules: ReturnType<typeof emailRule>[]) {
  stubFetch(alertsBackend({ rules }).route);
  return renderApp("/alerts");
}
