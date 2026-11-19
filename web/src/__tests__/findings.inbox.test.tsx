import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { formatLocalTime } from "../format";
import { dialogs } from "../pages/admin/ui";
import { debounce } from "../pages/findings/parts";
import { WORKSPACE_HEADER } from "../workspace";
import { GROUPS, SERIES_A, T0, backend, fakeLayout, finding, listRequests } from "./findingsFake";
import { json, renderApp } from "./helpers";
import { asRole } from "./sourcesFake";

const THREE = [finding(1), finding(2, { severity: "critical" }), finding(3)];
const DEBOUNCE_MS = debounce.ms;

beforeEach(() => {
  debounce.ms = 20;
  fakeLayout();
});
afterEach(() => {
  debounce.ms = DEBOUNCE_MS;
});

/** The rows of the findings table, without the header. */
async function rows(name = "Findings"): Promise<HTMLElement[]> {
  const table = await screen.findByRole("table", { name });
  return within(table).getAllByRole("row").slice(1);
}

/** Tick the checkbox of the finding with this summary. */
async function select(user: ReturnType<typeof userEvent.setup>, summary: string) {
  const row = screen.getByText(summary).closest('[role="row"]') as HTMLElement;
  await user.click(within(row).getByRole("checkbox"));
}

const status = () => screen.getByRole("status").textContent;
const searchOf = (app: ReturnType<typeof renderApp>) => app.router.state.location.search as Record<string, unknown>;

describe("findings.inbox", () => {
  it("lists open and acknowledged findings by default, each linking to its series", async () => {
    const asked = backend([
      finding(1, { assignee: { id: "u2", display_name: "Badr" } }),
      finding(2, { status: "muted", muted_until: "2026-11-20T11:00:00Z", feedback: "false_positive" }),
    ]);
    renderApp("/findings");
    const [first, second] = await rows();
    expect(asked.gets).toContain("/api/findings?limit=200");
    expect(within(first as HTMLElement).getAllByRole("cell").map((c) => c.textContent)).toEqual([
      "",
      formatLocalTime(T0 - 3_600e9),
      "Feeder 4 pressure",
      "tby.spikes",
      "high",
      "open",
      "Badr",
      "Spike number 1",
    ]);
    expect(within(first as HTMLElement).getByRole("link", { name: "Feeder 4 pressure" }).getAttribute("href")).toBe(`/series/${SERIES_A}`);
    expect(within(second as HTMLElement).getAllByRole("cell")[5]?.textContent).toBe("mutedFPfalse positiveuntil 2026-11-20 14:00");
    expect(screen.getByText("All 2 loaded")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Findings" }).getAttribute("href")).toBe("/findings");
    // Open and acknowledged are ticked without a parameter.
    const filters = screen.getByRole("search", { name: "Filter findings" });
    expect((within(filters).getByRole("checkbox", { name: "open" }) as HTMLInputElement).checked).toBe(true);
    expect((within(filters).getByRole("checkbox", { name: "acknowledged" }) as HTMLInputElement).checked).toBe(true);
    expect((within(filters).getByRole("checkbox", { name: "muted" }) as HTMLInputElement).checked).toBe(false);
  });

  it("writes the filters to the URL and sends them with the request", async () => {
    const asked = backend(THREE);
    const user = userEvent.setup();
    const app = renderApp("/findings");
    await rows();
    const filters = screen.getByRole("search", { name: "Filter findings" });

    await user.click(within(filters).getByRole("checkbox", { name: "acknowledged" }));
    await waitFor(() => expect(searchOf(app)).toEqual({ status: "open" }));
    await user.click(within(filters).getByRole("checkbox", { name: "critical" }));
    await user.click(within(filters).getByRole("checkbox", { name: "high" }));
    await user.selectOptions(within(filters).getByRole("combobox", { name: "Assignee" }), "Me");
    await user.selectOptions(within(filters).getByRole("combobox", { name: "Dimension" }), "plausibility");
    await user.selectOptions(within(filters).getByRole("combobox", { name: "Feedback" }), "No feedback");
    await waitFor(() =>
      expect(searchOf(app)).toEqual({ status: "open", severity: "critical,high", assignee: "me", dimension: "plausibility", feedback: "none" }),
    );
    await waitFor(() =>
      expect(asked.gets).toContain("/api/findings?status=open&severity=critical%2Chigh&dimension=plausibility&assignee=me&feedback=none&limit=200"),
    );

    // Text goes to the URL once the typing pauses, not with every key.
    await user.type(within(filters).getByRole("textbox", { name: "Search" }), "feeder 4");
    await waitFor(() => expect(searchOf(app).q).toBe("feeder 4"));
    const withText = listRequests(asked).filter((q) => q.has("q"));
    expect(withText.map((q) => q.get("q"))).toEqual(["feeder 4"]);
    await user.type(within(filters).getByRole("textbox", { name: "Check" }), "tby.gaps");
    await waitFor(() => expect(listRequests(asked).at(-1)?.get("check_id")).toBe("tby.gaps"));

    await user.click(within(filters).getByRole("button", { name: "Clear filters" }));
    await waitFor(() => expect(searchOf(app)).toEqual({}));
    expect((within(filters).getByRole("textbox", { name: "Search" }) as HTMLInputElement).value).toBe("");
    expect((within(filters).getByRole("button", { name: "Clear filters" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("reads the filters from the URL and follows Back", async () => {
    const asked = backend(THREE);
    const user = userEvent.setup();
    const app = renderApp("/findings?status=all&severity=critical&q=valve");
    await rows();
    expect(asked.gets).toContain("/api/findings?status=all&severity=critical&q=valve&limit=200");
    const filters = screen.getByRole("search", { name: "Filter findings" });
    expect((within(filters).getByRole("checkbox", { name: "resolved" }) as HTMLInputElement).checked).toBe(true);
    expect((within(filters).getByRole("textbox", { name: "Search" }) as HTMLInputElement).value).toBe("valve");

    await user.click(within(filters).getByRole("checkbox", { name: "critical" }));
    await waitFor(() => expect(searchOf(app)).toEqual({ status: "all", q: "valve" }));
    app.router.history.back();
    await waitFor(() => expect(searchOf(app)).toEqual({ status: "all", severity: "critical", q: "valve" }));
    expect((within(filters).getByRole("checkbox", { name: "critical" }) as HTMLInputElement).checked).toBe(true);
  });

  it("groups by series and lists the open group", async () => {
    const asked = backend(THREE);
    const user = userEvent.setup();
    const app = renderApp("/findings?severity=critical");
    await rows();
    await user.selectOptions(screen.getByRole("combobox", { name: "Group by" }), "Series");
    const list = await screen.findByRole("list", { name: "Findings by series" });
    expect(asked.gets).toContain("/api/findings/groups?by=series&severity=critical");
    expect(searchOf(app)).toEqual({ severity: "critical", group: "series" });
    expect(screen.queryByRole("table")).toBeNull();
    const groups = within(list).getAllByRole("button");
    expect(groups.map((g) => g.textContent)).toEqual([
      `▸Feeder 4 pressure12 findingscritical×2high×10latest ${formatLocalTime(T0)}`,
      `▸Feeder 5 flow1 findinglow×1latest ${formatLocalTime(T0 - 3_600e9)}`,
    ]);
    expect(groups[0]?.getAttribute("aria-expanded")).toBe("false");

    await user.click(groups[0] as HTMLElement);
    expect(await rows("Findings of Feeder 4 pressure")).toHaveLength(3);
    expect(searchOf(app)).toEqual({ severity: "critical", group: "series", open: SERIES_A });
    expect(asked.gets).toContain(`/api/findings?severity=critical&series_id=${SERIES_A}&limit=200`);
    expect(groups[0]?.getAttribute("aria-expanded")).toBe("true");

    // One group is open at a time; opening the open one closes it.
    await user.click(groups[1] as HTMLElement);
    await waitFor(() => expect(searchOf(app).open).toBe(GROUPS[1]?.key));
    expect(screen.getAllByRole("table")).toHaveLength(1);
    await user.click(groups[1] as HTMLElement);
    await waitFor(() => expect(screen.queryByRole("table")).toBeNull());
    expect(searchOf(app)).toEqual({ severity: "critical", group: "series" });
  });

  it("opens the group without an asset from the URL", async () => {
    const asked = backend(THREE);
    asked.groups = [{ key: "", label: "", count: 3, severities: { high: 3 }, latest: T0 }];
    renderApp("/findings?group=asset&open=");
    expect(await rows("Findings of No asset")).toHaveLength(3);
    expect(asked.gets).toContain("/api/findings?asset_path=&limit=200");
  });

  it("acknowledges the selected findings and says how many", async () => {
    const asked = backend(THREE, asRole("editor"));
    const user = userEvent.setup();
    renderApp("/findings");
    await rows();
    expect(screen.queryByRole("group", { name: "Actions on the selected findings" })).toBeNull();
    await select(user, "Spike number 1");
    await select(user, "Spike number 3");
    const bar = screen.getByRole("group", { name: "Actions on the selected findings" });
    expect(bar.textContent).toContain("2 selected");
    const before = listRequests(asked).length;

    await user.click(within(bar).getByRole("button", { name: "Acknowledge" }));
    await waitFor(() => expect(status()).toBe("2 findings acknowledged."));
    expect(asked.bulks).toEqual([{ ids: ["f-1", "f-3"], change: { status: "acked" } }]);
    expect(screen.getByRole("status").getAttribute("aria-live")).toBe("polite");
    // The selection is gone and the list was asked again.
    expect(screen.queryByRole("group", { name: "Actions on the selected findings" })).toBeNull();
    expect(screen.getAllByRole("checkbox", { checked: true }).map((c) => c.getAttribute("aria-label"))).not.toContain("Select all loaded findings");
    await waitFor(() => expect(listRequests(asked).length).toBeGreaterThan(before));
  });

  it("selects all loaded findings with the header checkbox, in three states", async () => {
    const asked = backend(THREE, asRole("editor"));
    const user = userEvent.setup();
    renderApp("/findings");
    await rows();
    const all = screen.getByRole("checkbox", { name: "Select all loaded findings" }) as HTMLInputElement;
    await select(user, "Spike number 2");
    expect(all.indeterminate).toBe(true);
    expect(all.checked).toBe(false);
    await user.click(all);
    expect(all.indeterminate).toBe(false);
    expect(all.checked).toBe(true);
    expect(screen.getByRole("group", { name: "Actions on the selected findings" }).textContent).toContain("3 selected");
    await user.click(screen.getByRole("button", { name: "Reopen" }));
    await waitFor(() => expect(status()).toBe("3 findings reopened."));
    expect(asked.bulks).toEqual([{ ids: ["f-1", "f-2", "f-3"], change: { status: "open" } }]);

    await user.click(all);
    await user.click(all);
    expect(screen.queryByRole("group", { name: "Actions on the selected findings" })).toBeNull();
  });

  it("drops the selection when the filter changes", async () => {
    backend(THREE, asRole("editor"));
    const user = userEvent.setup();
    renderApp("/findings");
    await rows();
    await select(user, "Spike number 1");
    expect(screen.getByRole("group", { name: "Actions on the selected findings" })).toBeTruthy();
    await user.click(screen.getByRole("checkbox", { name: "muted" }));
    await waitFor(() => expect(screen.queryByRole("group", { name: "Actions on the selected findings" })).toBeNull());
    await rows();
    expect(screen.queryAllByRole("checkbox", { name: /^Select Feeder/, checked: true })).toHaveLength(0);
  });

  it("keeps what a failed later request left over selected, and says what was done", async () => {
    // 501 selected findings go out as 500 and 1; the second request fails.
    const many = Array.from({ length: 501 }, (_, i) => finding(i + 1));
    const asked = backend(many, asRole("editor"));
    asked.bulk = (ids) => (ids.length === 500 ? { updated: ids, failed: [] } : json({ detail: "cache unavailable" }, 503));
    const user = userEvent.setup();
    renderApp("/findings");
    await rows();
    await user.click(screen.getByRole("checkbox", { name: "Select all loaded findings" }));
    await user.click(screen.getByRole("button", { name: "Acknowledge" }));
    await waitFor(() =>
      expect(status()).toBe("500 findings acknowledged. 1 got no answer and does not show the change now: it stays selected. The request failed: cache unavailable."),
    );
    // The finding without an answer was looked up and found as it was.
    expect(asked.gets).toContain("/api/findings/f-501");
    expect(asked.bulks.map((b) => b.ids.length)).toEqual([500, 1]);
    // The list was asked again, and only the finding that does not show the change is still selected: a retry sends it alone.
    expect(within(screen.getByRole("group", { name: "Actions on the selected findings" })).getByText("1 selected")).toBeTruthy();
    asked.bulk = (ids) => ({ updated: ids, failed: [] });
    await user.click(screen.getByRole("button", { name: "Acknowledge" }));
    await waitFor(() => expect(status()).toBe("1 finding acknowledged."));
    expect(asked.bulks[2]?.ids).toEqual(asked.bulks[1]?.ids);
  });

  it("looks up what a lost answer left unknown, also when the finding left the list", async () => {
    const asked = backend(THREE, asRole("editor"));
    // The server mutes the finding, so the default filter no longer lists it, and the
    // response never arrives.
    asked.bulk = () => {
      asked.items = [finding(2, { severity: "critical" }), finding(3)];
      asked.find = (id) => (id === "f-1" ? finding(1, { status: "muted", status_reason: "sensor swap" }) : undefined);
      throw new TypeError("Failed to fetch");
    };
    const user = userEvent.setup();
    renderApp("/findings");
    await rows();
    await select(user, "Spike number 1");
    await select(user, "Spike number 3");
    await user.click(screen.getByRole("button", { name: "Mute…" }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByRole("textbox", { name: "Reason" }), "sensor swap");
    await user.click(within(dialog).getByRole("button", { name: "Mute" }));
    // One was muted after all, one no longer exists: both are said, and nothing stays selected.
    await waitFor(() =>
      expect(status()).toBe(
        "No findings muted. 1 could not be changed: 1 no longer exists. 1 finding got no answer and is now muted. The request failed: Failed to fetch.",
      ),
    );
    expect(asked.gets).toEqual(expect.arrayContaining(["/api/findings/f-1", "/api/findings/f-3"]));
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(screen.queryByText("Spike number 1")).toBeNull());
    expect(screen.queryByRole("group", { name: "Actions on the selected findings" })).toBeNull();
  });

  it("says what a finding is now when the lookup does not show the change", async () => {
    // The request may have failed, or it muted the finding and someone reopened it since:
    // the lookup cannot tell, so the message says only what it shows.
    const asked = backend(THREE, asRole("editor"));
    asked.bulk = () => {
      throw new TypeError("Failed to fetch");
    };
    const user = userEvent.setup();
    renderApp("/findings");
    await rows();
    await select(user, "Spike number 1");
    const before = listRequests(asked).length;
    await user.click(screen.getByRole("button", { name: "Mute…" }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByRole("textbox", { name: "Reason" }), "sensor swap");
    await user.click(within(dialog).getByRole("button", { name: "Mute" }));
    await waitFor(() =>
      expect(status()).toBe("1 got no answer and does not show the change now: it stays selected. The request failed: Failed to fetch."),
    );
    expect(asked.gets).toContain("/api/findings/f-1");
    await waitFor(() => expect(listRequests(asked).length).toBeGreaterThan(before));
    expect(screen.getByText("1 selected")).toBeTruthy();
  });

  it("keeps the findings of a running change selected when a reload drops them", async () => {
    const asked = backend(THREE, asRole("editor"));
    let fail: (reason: unknown) => void = () => undefined;
    asked.bulk = () => new Promise<Response>((_, reject) => (fail = reject)) as unknown as Response;
    asked.find = () => {
      throw new TypeError("Failed to fetch");
    };
    const user = userEvent.setup();
    const app = renderApp("/findings");
    await rows();
    await select(user, "Spike number 1");
    await user.click(screen.getByRole("button", { name: "Acknowledge" }));
    await waitFor(() => expect(asked.bulks).toHaveLength(1));
    // While the request runs, a reload no longer lists the finding.
    asked.items = [finding(2, { severity: "critical" }), finding(3)];
    await app.client.invalidateQueries();
    await waitFor(() => expect(screen.queryByText("Spike number 1")).toBeNull());
    fail(new TypeError("Failed to fetch"));
    await waitFor(() => expect(status()).toMatch(/^1 got no answer and could not be checked: it stays selected/));
    expect(screen.getByText("1 selected")).toBeTruthy();
    // The header checkbox works on the loaded rows and leaves the unknown one selected.
    await user.click(screen.getByRole("checkbox", { name: "Select all loaded findings" }));
    expect(screen.getByText("3 selected")).toBeTruthy();
    await user.click(screen.getByRole("checkbox", { name: "Select all loaded findings" }));
    expect(screen.getByText("1 selected")).toBeTruthy();
  });

  it("keeps findings selected that got no answer and could not be looked up, also off the list", async () => {
    const asked = backend(THREE, asRole("editor"));
    // The server resolves the finding; neither the response nor the lookup gets through.
    asked.bulk = () => {
      asked.items = [finding(2, { severity: "critical" }), finding(3)];
      asked.find = () => {
        throw new TypeError("Failed to fetch");
      };
      throw new TypeError("Failed to fetch");
    };
    const user = userEvent.setup();
    renderApp("/findings");
    await rows();
    await select(user, "Spike number 1");
    await user.click(screen.getByRole("button", { name: "Resolve…" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Resolve" }));
    await waitFor(() =>
      expect(status()).toBe(
        "1 got no answer and could not be checked: it stays selected, also when the list no longer shows it. The request failed: Failed to fetch.",
      ),
    );
    // The reloaded list no longer shows it; it is still selected, and trying again says what it is.
    await waitFor(() => expect(screen.queryByText("Spike number 1")).toBeNull());
    expect(screen.getByText("1 selected")).toBeTruthy();
    asked.bulk = (ids) => ({ updated: [], failed: ids.map((id) => ({ id, error: "transition", status: "resolved" })) });
    await user.click(screen.getByRole("button", { name: "Resolve…" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Resolve" }));
    await waitFor(() => expect(status()).toBe("No findings resolved. 1 could not be changed: 1 is resolved."));
    expect(asked.bulks[1]?.ids).toEqual(["f-1"]);
    expect(screen.queryByRole("group", { name: "Actions on the selected findings" })).toBeNull();
  });

  it("does not take a finding that already was as asked for one the request changed", async () => {
    // Muted before, muted again with another reason, and the response is lost: the lookup
    // finds it muted, but not with what was sent, so it counts as not changed.
    const asked = backend([finding(1, { status: "muted", status_reason: "old reason" }), finding(2)], asRole("editor"));
    asked.bulk = () => {
      throw new TypeError("Failed to fetch");
    };
    const user = userEvent.setup();
    renderApp("/findings?status=muted");
    await rows();
    await select(user, "Spike number 1");
    await user.click(screen.getByRole("button", { name: "Mute…" }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByRole("textbox", { name: "Reason" }), "new reason");
    await user.click(within(dialog).getByRole("button", { name: "Mute" }));
    await waitFor(() =>
      expect(status()).toBe("1 got no answer and does not show the change now: it stays selected. The request failed: Failed to fetch."),
    );
    expect(asked.gets).toContain("/api/findings/f-1");
    expect(screen.getByText("1 selected")).toBeTruthy();
  });

  it("does not look findings up after the server refused the request", async () => {
    const asked = backend(THREE, asRole("editor"));
    asked.bulk = () => json({ detail: "Your role does not allow this." }, 403);
    const user = userEvent.setup();
    renderApp("/findings");
    await rows();
    await select(user, "Spike number 1");
    await user.click(screen.getByRole("button", { name: "Acknowledge" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/^Could not change the findings: /);
    expect(asked.gets.some((url) => url.startsWith("/api/findings/f-"))).toBe(false);
  });

  it("keeps a finding selected that was ticked while a change was running", async () => {
    const asked = backend(THREE, asRole("editor"));
    let answer: (value: Response) => void = () => undefined;
    asked.bulk = () => new Promise<Response>((resolve) => (answer = resolve)) as unknown as Response;
    const user = userEvent.setup();
    renderApp("/findings");
    await rows();
    await select(user, "Spike number 1");
    await user.click(screen.getByRole("button", { name: "Acknowledge" }));
    await waitFor(() => expect(asked.bulks).toHaveLength(1));
    await select(user, "Spike number 2");
    answer(json({ updated: ["f-1"], failed: [] }));
    await waitFor(() => expect(status()).toBe("1 finding acknowledged."));
    expect(asked.bulks[0]?.ids).toEqual(["f-1"]);
    expect(screen.getByText("1 selected")).toBeTruthy();
    const row = screen.getByText("Spike number 2").closest('[role="row"]') as HTMLElement;
    expect((within(row).getByRole("checkbox") as HTMLInputElement).checked).toBe(true);
  });

  it("does not select again a finding that left the list and came back", async () => {
    const asked = backend(THREE, asRole("editor"));
    const user = userEvent.setup();
    const app = renderApp("/findings");
    await rows();
    await select(user, "Spike number 1");
    await select(user, "Spike number 2");
    expect(screen.getByText("2 selected")).toBeTruthy();
    // A reload without the first finding, then one with it again.
    asked.items = [finding(2, { severity: "critical" }), finding(3)];
    await app.client.invalidateQueries();
    await waitFor(() => expect(screen.getByText("1 selected")).toBeTruthy());
    asked.items = THREE;
    await app.client.invalidateQueries();
    await waitFor(async () => expect(await rows()).toHaveLength(3));
    expect(screen.getByText("1 selected")).toBeTruthy();
    const back = screen.getByText("Spike number 1").closest('[role="row"]') as HTMLElement;
    expect((within(back).getByRole("checkbox") as HTMLInputElement).checked).toBe(false);
  });

  it("keeps a dialog and its Cancel while its request runs", async () => {
    const asked = backend(THREE, asRole("editor"));
    let answer: (value: Response) => void = () => undefined;
    asked.bulk = () => new Promise<Response>((resolve) => (answer = resolve)) as unknown as Response;
    const user = userEvent.setup();
    renderApp("/findings");
    await rows();
    await select(user, "Spike number 1");
    await user.click(screen.getByRole("button", { name: "Resolve…" }));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Resolve" }));
    await waitFor(() => expect((within(dialog).getByRole("button", { name: "Cancel" }) as HTMLButtonElement).disabled).toBe(true));
    await user.keyboard("{Escape}");
    expect(screen.getByRole("dialog")).toBeTruthy();
    answer(json({ updated: ["f-1"], failed: [] }));
    await waitFor(() => expect(status()).toBe("1 finding resolved."));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(asked.bulks).toHaveLength(1);
  });

  it("does not put typed text over a saved filter applied while it was waiting", async () => {
    debounce.ms = 150;
    const asked = backend(THREE, asRole("viewer"));
    asked.filters = [{ id: "sf-1", name: "Feeders", query: "q=feeder", created_at: "2026-11-01T00:00:00Z" }];
    const user = userEvent.setup();
    const app = renderApp("/findings");
    await rows();
    await user.click(await screen.findByRole("button", { name: "Saved filters (1)" }));
    // Both events are dispatched in the same task, so the typed text is still waiting when the
    // saved filter is applied, however slow the runner is.
    const feeders = screen.getByRole("button", { name: "Feeders" });
    fireEvent.change(screen.getByRole("textbox", { name: "Search" }), { target: { value: "pump" } });
    fireEvent.click(feeders);
    await waitFor(() => expect(searchOf(app)).toEqual({ q: "feeder" }));
    expect((screen.getByRole("textbox", { name: "Search" }) as HTMLInputElement).value).toBe("feeder");
    await new Promise((resolve) => setTimeout(resolve, 250));
    expect(searchOf(app)).toEqual({ q: "feeder" });
    expect(listRequests(asked).some((query) => query.get("q") === "pump")).toBe(false);
  });

  it("names what could not be changed", async () => {
    const asked = backend(THREE, asRole("editor"));
    asked.bulk = () => ({
      updated: ["f-1"],
      failed: [
        { id: "f-2", error: "transition", status: "resolved" },
        { id: "f-3", error: "not_found", status: null },
      ],
    });
    const user = userEvent.setup();
    renderApp("/findings");
    await rows();
    await user.click(screen.getByRole("checkbox", { name: "Select all loaded findings" }));
    await user.click(screen.getByRole("button", { name: "Acknowledge" }));
    await waitFor(() => expect(status()).toBe("1 finding acknowledged. 2 could not be changed: 1 is resolved, 1 no longer exists."));
  });

  it("mutes with a required reason and an optional end", async () => {
    const asked = backend(THREE, asRole("editor"));
    const user = userEvent.setup();
    renderApp("/findings");
    await rows();
    await select(user, "Spike number 2");
    const opener = screen.getByRole("button", { name: "Mute…" });
    await user.click(opener);
    let dialog = screen.getByRole("dialog", { name: "Mute 1 finding" });
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    const reason = within(dialog).getByRole("textbox", { name: "Reason" });
    expect(document.activeElement).toBe(reason);

    // No reason: nothing is sent.
    await user.click(within(dialog).getByRole("button", { name: "Mute" }));
    expect(within(dialog).getByRole("alert").textContent).toBe("Give a reason: it is what the audit log keeps.");
    expect(asked.bulks).toHaveLength(0);

    // Escape closes it and the focus returns to the button that opened it.
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(opener);

    await user.click(opener);
    dialog = screen.getByRole("dialog", { name: "Mute 1 finding" });
    await user.type(within(dialog).getByRole("textbox", { name: "Reason" }), "sensor swap on feeder 4");
    const until = within(dialog).getByLabelText(/^Until/);
    fireEvent.change(until, { target: { value: "2020-01-01T10:00" } });
    await user.click(within(dialog).getByRole("button", { name: "Mute" }));
    expect(within(dialog).getByRole("alert").textContent).toBe("The end must lie in the future.");
    expect(asked.bulks).toHaveLength(0);

    // Local time in the box, an instant in the request.
    const end = new Date(Date.now() + 3 * 86_400_000);
    const pad = (n: number) => String(n).padStart(2, "0");
    const local = `${end.getFullYear()}-${pad(end.getMonth() + 1)}-${pad(end.getDate())}T${pad(end.getHours())}:${pad(end.getMinutes())}`;
    fireEvent.change(until, { target: { value: local } });
    await user.click(within(dialog).getByRole("button", { name: "Mute" }));
    await waitFor(() => expect(status()).toBe("1 finding muted."));
    expect(asked.bulks).toEqual([
      { ids: ["f-2"], change: { status: "muted", reason: "sensor swap on feeder 4", muted_until: new Date(local).toISOString() } },
    ]);
    expect(asked.bulks[0]?.change.muted_until).toMatch(/^\d{4}-\d\d-\d\dT\d\d:\d\d:00\.000Z$/);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("mutes without an end", async () => {
    const asked = backend(THREE, asRole("editor"));
    const user = userEvent.setup();
    renderApp("/findings");
    await rows();
    await select(user, "Spike number 1");
    await user.click(screen.getByRole("button", { name: "Mute…" }));
    await user.type(screen.getByRole("textbox", { name: "Reason" }), "known");
    await user.keyboard("{Enter}");
    await waitFor(() => expect(status()).toBe("1 finding muted."));
    expect(asked.bulks).toEqual([{ ids: ["f-1"], change: { status: "muted", reason: "known" } }]);
  });

  it("resolves with an optional reason", async () => {
    const asked = backend(THREE, asRole("editor"));
    const user = userEvent.setup();
    renderApp("/findings");
    await rows();
    await select(user, "Spike number 1");
    await user.click(screen.getByRole("button", { name: "Resolve…" }));
    let dialog = screen.getByRole("dialog", { name: "Resolve 1 finding" });
    await user.click(within(dialog).getByRole("button", { name: "Resolve" }));
    await waitFor(() => expect(status()).toBe("1 finding resolved."));

    await select(user, "Spike number 2");
    await user.click(screen.getByRole("button", { name: "Resolve…" }));
    dialog = screen.getByRole("dialog", { name: "Resolve 1 finding" });
    await user.type(within(dialog).getByRole("textbox", { name: "Reason (optional)" }), " valve replaced ");
    await user.click(within(dialog).getByRole("button", { name: "Resolve" }));
    await waitFor(() => expect(asked.bulks).toHaveLength(2));
    expect(asked.bulks).toEqual([
      { ids: ["f-1"], change: { status: "resolved" } },
      { ids: ["f-2"], change: { status: "resolved", reason: "valve replaced" } },
    ]);
  });

  it("assigns to a colleague and unassigns", async () => {
    const asked = backend(THREE, asRole("editor"));
    const user = userEvent.setup();
    renderApp("/findings");
    await rows();
    await select(user, "Spike number 1");
    await select(user, "Spike number 2");
    await user.click(screen.getByRole("button", { name: "Assign…" }));
    let dialog = screen.getByRole("dialog", { name: "Assign 2 findings" });
    const submit = within(dialog).getByRole("button", { name: "Assign" }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Assign to" }), await within(dialog).findByRole("option", { name: "Badr" }));
    await user.click(submit);
    await waitFor(() => expect(status()).toBe("2 findings assigned to Badr."));

    await select(user, "Spike number 3");
    await user.click(screen.getByRole("button", { name: "Assign…" }));
    dialog = screen.getByRole("dialog", { name: "Assign 1 finding" });
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Assign to" }), "Unassign");
    await user.click(within(dialog).getByRole("button", { name: "Assign" }));
    await waitFor(() => expect(status()).toBe("1 finding unassigned."));
    expect(asked.bulks).toEqual([
      { ids: ["f-1", "f-2"], change: { assignee_id: "u2" } },
      { ids: ["f-3"], change: { assignee_id: null } },
    ]);
  });

  it("marks and unmarks false positives", async () => {
    const asked = backend(THREE, asRole("editor"));
    const user = userEvent.setup();
    renderApp("/findings");
    await rows();
    await select(user, "Spike number 1");
    await user.click(screen.getByRole("button", { name: "False positive" }));
    await waitFor(() => expect(status()).toBe("1 finding marked as false positive."));
    await select(user, "Spike number 1");
    await user.click(screen.getByRole("button", { name: "Remove false positive" }));
    await waitFor(() => expect(status()).toBe("1 finding no longer marked as false positive."));
    expect(asked.bulks.map((b) => b.change)).toEqual([{ feedback: "false_positive" }, { feedback: null }]);
  });

  it("shows a refused change and keeps the selection", async () => {
    const asked = backend(THREE, asRole("editor"));
    asked.bulk = () => json({ detail: [{ loc: ["body", "change", "muted_until"], msg: "must lie in the future" }] }, 422);
    const user = userEvent.setup();
    renderApp("/findings");
    await rows();
    await select(user, "Spike number 1");
    await user.click(screen.getByRole("button", { name: "Mute…" }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByRole("textbox", { name: "Reason" }), "known");
    await user.click(within(dialog).getByRole("button", { name: "Mute" }));
    expect((await within(dialog).findByRole("alert")).textContent).toBe("Could not change the findings: change.muted_until: must lie in the future");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    asked.bulk = () => json({ detail: "forbidden" }, 403);
    await user.click(screen.getByRole("button", { name: "Acknowledge" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Could not change the findings: Your role does not allow this.");
    expect(screen.getByRole("group", { name: "Actions on the selected findings" }).textContent).toContain("1 selected");
    expect(status()).toBe("");
  });

  it("gives viewers no checkboxes and no actions, but filters, groups, saved filters and the export", async () => {
    backend(THREE, asRole("viewer"));
    renderApp("/findings");
    const [first] = await rows();
    // The role has arrived when the editor's checkboxes would have.
    await new Promise((resolve) => setTimeout(resolve, 20));
    const table = screen.getByRole("table", { name: "Findings" });
    expect(within(table).queryByRole("checkbox")).toBeNull();
    expect(within(first as HTMLElement).getAllByRole("cell")).toHaveLength(7);
    expect(screen.queryByRole("group", { name: "Actions on the selected findings" })).toBeNull();
    expect(screen.getByRole("search", { name: "Filter findings" })).toBeTruthy();
    expect(screen.getByRole("combobox", { name: "Group by" })).toBeTruthy();
    expect(screen.getByRole("button", { name: /^Saved filters/ })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Export CSV" })).toBeTruthy();
  });

  it("lists, applies, saves and deletes saved filters", async () => {
    const asked = backend(THREE, asRole("viewer"));
    asked.filters = [{ id: "sf-1", name: "Open, critical, mine", query: "status=open&severity=critical&assignee=me&group=series", created_at: "2026-11-01T00:00:00Z" }];
    const user = userEvent.setup();
    const app = renderApp("/findings?severity=high&q=feeder");
    await rows();
    const menu = await screen.findByRole("button", { name: "Saved filters (1)" });
    expect(menu.getAttribute("aria-expanded")).toBe("false");
    await user.click(menu);
    let panel = screen.getByRole("region", { name: "Saved filters" });
    expect(within(within(panel).getByRole("list", { name: "Your filters" })).getAllByRole("listitem")).toHaveLength(1);

    // Saving stores the page's search string.
    await user.type(within(panel).getByRole("textbox", { name: "Save current filter as" }), "High feeders");
    await user.click(within(panel).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(within(panel).getAllByRole("listitem")).toHaveLength(2));
    expect(asked.saves).toEqual([{ name: "High feeders", query: "severity=high&q=feeder" }]);
    expect((within(panel).getByRole("textbox", { name: "Save current filter as" }) as HTMLInputElement).value).toBe("");

    // A taken name and the limit are said in words.
    asked.save = () => json({ detail: "name_taken" }, 409);
    await user.type(within(panel).getByRole("textbox", { name: "Save current filter as" }), "High feeders");
    await user.click(within(panel).getByRole("button", { name: "Save" }));
    expect((await within(panel).findByRole("alert")).textContent).toBe("You already have a filter with this name.");
    asked.save = () => json({ detail: "too_many" }, 409);
    await user.click(within(panel).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(within(panel).getByRole("alert").textContent).toBe("You have reached the limit of 50 saved filters. Delete one first."));

    // Applying one replaces the URL's search.
    await user.click(within(panel).getByRole("button", { name: "Open, critical, mine" }));
    await waitFor(() => expect(searchOf(app)).toEqual({ status: "open", severity: "critical", assignee: "me", group: "series" }));
    expect(await screen.findByRole("list", { name: "Findings by series" })).toBeTruthy();
    expect(asked.gets).toContain("/api/findings/groups?by=series&status=open&severity=critical&assignee=me");
    expect(screen.queryByRole("region", { name: "Saved filters" })).toBeNull();

    // Deleting asks first.
    await user.click(screen.getByRole("button", { name: "Saved filters (2)" }));
    panel = screen.getByRole("region", { name: "Saved filters" });
    const confirm = vi.spyOn(dialogs, "confirm").mockReturnValue(false);
    await user.click(within(panel).getByRole("button", { name: "Delete Open, critical, mine" }));
    expect(confirm).toHaveBeenCalledWith('Delete the saved filter "Open, critical, mine"?');
    expect(asked.deleted).toEqual([]);
    confirm.mockReturnValue(true);
    await user.click(within(panel).getByRole("button", { name: "Delete Open, critical, mine" }));
    await waitFor(() => expect(within(panel).getAllByRole("listitem")).toHaveLength(1));
    expect(asked.deleted).toEqual(["sf-1"]);
  });

  it("exports the current filter, not the grouping", async () => {
    const asked = backend(THREE);
    const user = userEvent.setup();
    renderApp("/findings?status=open&severity=critical&group=check");
    const link = await screen.findByRole("link", { name: "Export CSV" });
    expect(link.getAttribute("href")).toBe("/api/findings/export?status=open&severity=critical");
    expect(link.hasAttribute("download")).toBe(true);

    // The click fetches the file in the chosen workspace and hands it to the browser.
    const created: Blob[] = [];
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: (blob: Blob) => (created.push(blob), "blob:x"), revokeObjectURL: () => undefined }));
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    await user.click(link);
    await waitFor(() => expect(created).toHaveLength(1));
    const call = asked.fetch.mock.calls.find(([url]) => url === "/api/findings/export?status=open&severity=critical");
    expect((call?.[1]?.headers as Record<string, string>)[WORKSPACE_HEADER]).toBe("w1");
  });

  it("loads the next page on request and says how many are loaded", async () => {
    const asked = backend([]);
    const page = (from: number) => Array.from({ length: 200 }, (_, i) => finding(from + i));
    asked.list = (query) => (query.get("cursor") === "c2" ? { items: page(200), next_cursor: null } : { items: page(0), next_cursor: "c2" });
    const user = userEvent.setup();
    renderApp("/findings");
    const table = await screen.findByRole("table", { name: "Findings" });
    expect(screen.getByText("200 loaded")).toBeTruthy();
    expect(table.getAttribute("aria-rowcount")).toBe("-1");
    // The rows drawn are far from the end: nothing was asked yet.
    expect(listRequests(asked).some((q) => q.has("cursor"))).toBe(false);
    await user.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByText("All 400 loaded")).toBeTruthy();
    expect(asked.gets).toContain("/api/findings?limit=200&cursor=c2");
    expect(table.getAttribute("aria-rowcount")).toBe("401");
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });

  it("asks for the next page when the rows drawn near the end", async () => {
    const asked = backend([]);
    asked.list = (query) =>
      query.get("cursor") === "c2" ? { items: [finding(31)], next_cursor: null } : { items: Array.from({ length: 30 }, (_, i) => finding(i)), next_cursor: "c2" };
    renderApp("/findings");
    expect(await screen.findByText("All 31 loaded")).toBeTruthy();
    expect(asked.gets).toContain("/api/findings?limit=200&cursor=c2");
  });

  it("says when nothing matches, and shows a load error", async () => {
    backend([]);
    const first = renderApp("/findings");
    expect(await screen.findByText("No findings match.")).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
    first.unmount();
    const asked = backend([]);
    asked.list = () => {
      throw new Error("boom");
    };
    renderApp("/findings");
    expect((await screen.findByRole("alert")).textContent).toMatch(/^Could not load findings: /);
  });
});
