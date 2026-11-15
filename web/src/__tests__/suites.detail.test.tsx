import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { dialogs } from "../pages/admin/ui";
import { json, makeRun, renderApp, stubFetch } from "./helpers";
import { asRole } from "./sourcesFake";
import { RUN_ID, SUITE_ID, bodyOf, suite } from "./suitesFake";

afterEach(() => vi.restoreAllMocks());

const NEW_RUN = "eeeeeeee-0000-0000-0000-000000000009";
const OLD_RUN = "eeeeeeee-0000-0000-0000-000000000002";
const RUNS = `/api/runs?suite_id=${SUITE_ID}&limit=25`;

type Sent = { method: string; url: string; body: unknown };

/** A fake backend for one suite; `runNow` answers "Run now". */
function backend(stored = suite(), runNow: () => unknown = () => json({ id: NEW_RUN, status: "queued", created_at: "2026-10-09T08:30:00Z" }, 202)) {
  const sent: Sent[] = [];
  let current = stored;
  const base = `/api/suites/${stored.id}`;
  const route = (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (method !== "GET") sent.push({ method, url, body: bodyOf(init) });
    if (url === base && method === "GET") return current;
    if (url === base && method === "PATCH") {
      current = { ...current, ...(bodyOf(init) as object) };
      return current;
    }
    if (url === base && method === "DELETE") return new Response(null, { status: 204 });
    if (url === `${base}/runs` && method === "POST") return runNow();
    if (url === RUNS) {
      return {
        items: [
          makeRun({ id: RUN_ID, trigger: "suite", suite_id: SUITE_ID, started_at: "2026-10-09T08:05:01Z", stats: { n_findings: 3 } }),
          makeRun({ id: OLD_RUN, trigger: "suite", suite_id: SUITE_ID, status: "failed", started_at: "2026-10-09T07:05:01Z", stats: {} }),
        ],
        next_cursor: "older",
      };
    }
    if (url === `${RUNS}&cursor=older`) {
      return { items: [makeRun({ id: "eeeeeeee-0000-0000-0000-000000000003", trigger: "manual", suite_id: SUITE_ID })], next_cursor: null };
    }
    if (url === "/api/suites?limit=100") return { items: [], next_cursor: null };
    throw new Error(`unexpected ${method} ${url}`);
  };
  return { route, sent };
}

describe("suites.detail", () => {
  it("shows the configuration and every threshold with its source", async () => {
    stubFetch(backend().route, asRole("editor"));
    renderApp(`/suites/${SUITE_ID}`);
    expect(await screen.findByRole("heading", { name: "Feeders, hourly" })).toBeTruthy();
    const config = within(screen.getByRole("region", { name: "Configuration" }));
    expect(config.getByText("Feeders")).toBeTruthy();
    expect(config.getByText("Every hour at :05")).toBeTruthy();
    expect(config.getByText("5 * * * *")).toBeTruthy();
    expect(config.getByText("2026-10-09 12:05:00 UTC+03:00")).toBeTruthy();
    expect(config.getByRole("link", { name: /succeeded/ }).getAttribute("href")).toBe(`/runs/${RUN_ID}`);

    const rowsOf = (check: string) =>
      within(screen.getByRole("table", { name: `Thresholds of ${check}` }))
        .getAllByRole("row")
        .slice(1)
        .map((row) => within(row).getAllByRole("cell").map((c) => c.textContent));
    expect(rowsOf("spike")).toEqual([
      ["threshold", "6", "override", ""],
      ["window", "25", "default", ""],
      ["two_sided", "true", "default", ""],
    ]);
    expect(rowsOf("range")).toEqual([
      ["min", "—", "metadata", "the series' physical minimum"],
      ["max", "—", "metadata", "the series' physical maximum"],
      ["mode", "physical", "default", ""],
      ["ignore", "[]", "default", ""],
    ]);
    expect(screen.queryByRole("table", { name: "Thresholds of drift" })).toBeNull();
    expect(screen.getByText("Switched off:").textContent).toBe("Switched off: drift");
  });

  it("names a baseline threshold of a check that runs", async () => {
    const stored = suite();
    stubFetch(backend({ ...stored, checks: stored.checks.map((c) => ({ ...c, enabled: true })) }).route);
    renderApp(`/suites/${SUITE_ID}`);
    const table = await screen.findByRole("table", { name: "Thresholds of drift" });
    expect(within(table).getByText("baseline")).toBeTruthy();
    expect(within(table).getByText("three times the baseline's spread")).toBeTruthy();
  });

  it("starts a run now and links to it", async () => {
    const { route, sent } = backend();
    stubFetch(route, asRole("editor"));
    const user = userEvent.setup();
    renderApp(`/suites/${SUITE_ID}`);
    await user.click(await screen.findByRole("button", { name: "Run now" }));
    const started = (await screen.findByText(/Run started:/)) as HTMLElement;
    expect(within(started).getByText("queued")).toBeTruthy();
    expect(within(started).getByRole("link").getAttribute("href")).toBe(`/runs/${NEW_RUN}`);
    expect(sent).toEqual([{ method: "POST", url: `/api/suites/${SUITE_ID}/runs`, body: null }]);
  });

  it("explains a 409 no_dataset, and says the dataset is gone", async () => {
    const { route } = backend(suite({ dataset_id: null, dataset_name: null, next_run_at: null }), () => json({ detail: "no_dataset" }, 409));
    stubFetch(route, asRole("editor"));
    const user = userEvent.setup();
    renderApp(`/suites/${SUITE_ID}`);
    await user.click(await screen.findByRole("button", { name: "Run now" }));
    expect(await screen.findByText("This suite has no dataset, so there is nothing to run. Edit the suite and choose one.")).toBeTruthy();
    expect(screen.getByText("dataset deleted")).toBeTruthy();
    expect(screen.getByText(/The schedule skips this suite/)).toBeTruthy();
    expect(screen.queryByText(/Run started/)).toBeNull();
  });

  it("lists the run history with status, time and a link to each report, and loads more", async () => {
    stubFetch(backend().route, asRole("viewer"));
    const user = userEvent.setup();
    renderApp(`/suites/${SUITE_ID}`);
    const table = await screen.findByRole("table", { name: "The suite's runs, newest first" });
    const rows = () => within(table).getAllByRole("row").slice(1) as HTMLElement[];
    expect(rows().map((row) => within(row).getAllByRole("cell").map((c) => c.textContent))).toEqual([
      ["succeeded", "2026-10-09 11:05:01 UTC+03:00", "suite", "3", "Open report"],
      ["failed", "2026-10-09 10:05:01 UTC+03:00", "suite", "—", "Open report"],
    ]);
    expect(rows().map((row) => within(row).getByRole("link").getAttribute("href"))).toEqual([`/runs/${RUN_ID}`, `/runs/${OLD_RUN}`]);
    await user.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(rows()).toHaveLength(3));
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });

  it("disables and enables the suite", async () => {
    const { route, sent } = backend();
    stubFetch(route, asRole("editor"));
    const user = userEvent.setup();
    renderApp(`/suites/${SUITE_ID}`);
    await user.click(await screen.findByRole("button", { name: "Disable" }));
    expect(await screen.findByText("(disabled)")).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Enable" }));
    await waitFor(() => expect(screen.queryByText("(disabled)")).toBeNull());
    expect(sent.map((s) => s.body)).toEqual([{ enabled: false }, { enabled: true }]);
  });

  it("deletes only after confirmation, then goes to the list", async () => {
    const { route, sent } = backend();
    stubFetch(route, asRole("editor"));
    const confirm = vi.spyOn(dialogs, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    const user = userEvent.setup();
    const { router } = renderApp(`/suites/${SUITE_ID}`);
    await user.click(await screen.findByRole("button", { name: "Delete" }));
    expect(sent).toEqual([]);
    await user.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/suites"));
    expect(confirm).toHaveBeenCalledWith('Delete the suite "Feeders, hourly"? Its runs stay.');
    expect(sent).toEqual([{ method: "DELETE", url: `/api/suites/${SUITE_ID}`, body: null }]);
  });

  it("links editors to the edit page", async () => {
    stubFetch(backend().route, asRole("editor"));
    renderApp(`/suites/${SUITE_ID}`);
    expect((await screen.findByRole("link", { name: "Edit" })).getAttribute("href")).toBe(`/suites/${SUITE_ID}/edit`);
  });

  it("shows viewers no write controls", async () => {
    stubFetch(backend().route, asRole("viewer"));
    renderApp(`/suites/${SUITE_ID}`);
    expect(await screen.findByRole("heading", { name: "Run history" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Checks and thresholds" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Actions" })).toBeNull();
    for (const name of ["Run now", "Disable", "Delete"]) expect(screen.queryByRole("button", { name })).toBeNull();
    expect(screen.queryByRole("link", { name: "Edit" })).toBeNull();
  });

  it("says when the suite does not exist", async () => {
    stubFetch(() => json({ detail: "not_found" }, 404));
    renderApp(`/suites/${SUITE_ID}`);
    expect(await screen.findByText("This suite does not exist (any more).")).toBeTruthy();
  });
});
