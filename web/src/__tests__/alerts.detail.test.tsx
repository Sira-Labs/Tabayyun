import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { dialogs } from "../pages/admin/ui";
import { json, renderApp, stubFetch } from "./helpers";
import { HOOK_ID, RULE_ID, alertsBackend, emailRule, hookRule, notification } from "./alertsFake";
import { asRole } from "./sourcesFake";

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

const ADMIN = asRole("admin");
const NOTES = `/api/alerts/${RULE_ID}/notifications?limit=50`;

const config = () => within(screen.getByRole("region", { name: "Rule" }));
const rowsOf = (table: HTMLElement) =>
  within(table)
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell").map((c) => c.textContent));

describe("alerts.detail", () => {
  it("shows the rule in words and its notifications, newest first", async () => {
    const notes = () => [
      notification({ id: "n2", kind: "test", due_at: "2026-10-09T09:00:00Z", n_matches: 1 }),
      notification({ id: "n1", status: "failed", attempts: 3, error: "HTTP 500", sent_at: null }),
      notification({ id: "n0", status: "skipped", attempts: 0, error: "SMTP is not configured", sent_at: null }),
    ];
    stubFetch(alertsBackend({ notes }).route, asRole("viewer"));
    renderApp(`/alerts/${RULE_ID}`);
    expect(await screen.findByRole("heading", { name: "Critical on feeders" })).toBeTruthy();
    expect(config().getByText("New findings of severity critical or higher on plant/feeder…")).toBeTruthy();
    expect(config().getByText("ops@example.org")).toBeTruthy();
    expect(config().getByText("1 hour")).toBeTruthy();
    expect(config().getByText("enabled")).toBeTruthy();
    const table = await screen.findByRole("table", { name: "The rule's notifications, newest first" });
    expect(rowsOf(table)).toEqual([
      ["2026-10-09 12:00:00 UTC+03:00", "test", "sent", "1", "1", ""],
      ["2026-10-09 11:01:00 UTC+03:00", "alert", "failed", "3", "3", "HTTP 500"],
      ["2026-10-09 11:01:00 UTC+03:00", "alert", "skipped", "3", "0", "SMTP is not configured"],
    ]);
  });

  it("shows a webhook rule's full URL to admins and its host to others", async () => {
    stubFetch(alertsBackend().route, ADMIN);
    const first = renderApp(`/alerts/${HOOK_ID}`);
    expect(await screen.findByText("https://hooks.example.com/tabayyun?k=1")).toBeTruthy();
    expect(config().getByText("signed with a secret")).toBeTruthy();
    expect(config().getByText("Series whose overall score falls below 70")).toBeTruthy();
    first.unmount();
    stubFetch(alertsBackend({ rules: [hookRule({ url: null })] }).route, asRole("editor"));
    renderApp(`/alerts/${HOOK_ID}`);
    expect(await screen.findByText("hooks.example.com")).toBeTruthy();
  });

  it("sends a test, says it is queued and reloads the notifications", async () => {
    let tested = false;
    const { route, sent } = alertsBackend({
      notes: () => (tested ? [notification({ id: "t", kind: "test", status: "sent" }), notification()] : [notification()]),
      answer: (s) => {
        if (s.url.endsWith("/test")) tested = true;
        return undefined;
      },
    });
    stubFetch(route, ADMIN);
    const user = userEvent.setup();
    renderApp(`/alerts/${RULE_ID}`);
    const table = await screen.findByRole("table", { name: "The rule's notifications, newest first" });
    expect(rowsOf(table)).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Send test" }));
    expect((await screen.findByRole("status")).textContent).toBe("Test notification queued.");
    await waitFor(() => expect(rowsOf(screen.getByRole("table", { name: "The rule's notifications, newest first" }))).toHaveLength(2));
    expect(sent).toEqual([{ method: "POST", url: `/api/alerts/${RULE_ID}/test`, body: null }]);
  });

  it("polls the notifications while one is waiting or sending", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let calls = 0;
    const fetch = stubFetch(
      alertsBackend({
        notes: () => {
          calls += 1;
          return [notification({ status: calls < 3 ? (calls === 1 ? "pending" : "sending") : "sent", sent_at: calls < 3 ? null : "2026-10-09T08:01:05Z" })];
        },
      }).route,
      ADMIN,
    );
    renderApp(`/alerts/${RULE_ID}`);
    expect(await screen.findByText("waiting to be sent")).toBeTruthy();
    await vi.advanceTimersByTimeAsync(5000);
    expect(await screen.findByText("sending")).toBeTruthy();
    await vi.advanceTimersByTimeAsync(5000);
    const table = await screen.findByRole("table", { name: "The rule's notifications, newest first" });
    await waitFor(() => expect(within(table).getByText("sent")).toBeTruthy());
    await vi.advanceTimersByTimeAsync(15000);
    expect(fetch.mock.calls.filter(([u]) => u === NOTES)).toHaveLength(3);
  });

  it("disables and enables the rule", async () => {
    const { route, sent } = alertsBackend();
    stubFetch(route, ADMIN);
    const user = userEvent.setup();
    renderApp(`/alerts/${RULE_ID}`);
    await user.click(await screen.findByRole("button", { name: "Disable" }));
    expect(await screen.findByText("(disabled)")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Enable" }));
    await waitFor(() => expect(screen.queryByText("(disabled)")).toBeNull());
    expect(sent.map((s) => [s.method, s.body])).toEqual([
      ["PATCH", { enabled: false }],
      ["PATCH", { enabled: true }],
    ]);
  });

  it("deletes only after confirmation, then goes to the list", async () => {
    const { route, sent } = alertsBackend();
    stubFetch(route, ADMIN);
    const confirm = vi.spyOn(dialogs, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    const user = userEvent.setup();
    const { router } = renderApp(`/alerts/${RULE_ID}`);
    await user.click(await screen.findByRole("button", { name: "Delete" }));
    expect(sent).toEqual([]);
    await user.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/alerts"));
    expect(confirm).toHaveBeenCalledWith('Delete the alert rule "Critical on feeders"? Its notifications are deleted with it.');
    expect(sent).toEqual([{ method: "DELETE", url: `/api/alerts/${RULE_ID}`, body: null }]);
    const table = await screen.findByRole("table", { name: "Alert rules by name" });
    expect(within(table).queryByText("Critical on feeders")).toBeNull();
  });

  it("links admins to the edit page", async () => {
    stubFetch(alertsBackend().route, ADMIN);
    renderApp(`/alerts/${RULE_ID}`);
    expect((await screen.findByRole("link", { name: "Edit" })).getAttribute("href")).toBe(`/alerts/${RULE_ID}/edit`);
  });

  it("shows viewers and editors everything but the actions", async () => {
    for (const role of ["viewer", "editor"] as const) {
      stubFetch(alertsBackend().route, asRole(role));
      const view = renderApp(`/alerts/${RULE_ID}`);
      expect(await screen.findByRole("table", { name: "The rule's notifications, newest first" })).toBeTruthy();
      expect(screen.getByRole("region", { name: "Rule" })).toBeTruthy();
      expect(screen.queryByRole("heading", { name: "Actions" })).toBeNull();
      for (const name of ["Send test", "Disable", "Delete"]) expect(screen.queryByRole("button", { name })).toBeNull();
      expect(screen.queryByRole("link", { name: "Edit" })).toBeNull();
      view.unmount();
    }
  });

  it("says when there are no notifications, and when the rule does not exist", async () => {
    stubFetch(alertsBackend({ rules: [emailRule({ enabled: false })], notes: () => [] }).route, ADMIN);
    const first = renderApp(`/alerts/${RULE_ID}`);
    expect(await screen.findByText(/No notifications yet/)).toBeTruthy();
    expect(screen.getByText("(disabled)")).toBeTruthy();
    first.unmount();
    stubFetch(() => json({ detail: "not_found" }, 404));
    renderApp(`/alerts/${RULE_ID}`);
    expect(await screen.findByText("This alert rule does not exist (any more).")).toBeTruthy();
  });
});
