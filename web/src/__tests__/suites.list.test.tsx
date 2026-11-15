import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { renderApp, stubFetch } from "./helpers";
import { asRole } from "./sourcesFake";
import { RUN_ID, SUITE_ID, suite } from "./suitesFake";

const OTHER_ID = "cccccccc-0000-0000-0000-000000000002";
const SUITES = {
  items: [
    suite(),
    suite({
      id: OTHER_ID,
      name: "Orphan",
      dataset_id: null,
      dataset_name: null,
      schedule: null,
      enabled: false,
      next_run_at: null,
      last_run_at: null,
      last_run: null,
    }),
  ],
  next_cursor: null,
};

const route = (url: string) => {
  if (url === "/api/suites?limit=100") return SUITES;
  throw new Error(`unexpected ${url}`);
};

describe("suites.list", () => {
  it("lists suites with dataset, schedule in words, next run, last run and state", async () => {
    stubFetch(route, asRole("editor"));
    renderApp("/suites");
    const table = await screen.findByRole("table", { name: "Suites by name" });
    const rows = within(table).getAllByRole("row").slice(1) as HTMLElement[];
    const cells = (row: HTMLElement) => within(row).getAllByRole("cell").map((c) => c.textContent);

    expect(cells(rows[0] as HTMLElement)).toEqual([
      "Feeders, hourly",
      "Feeders",
      "Every hour at :05",
      "2026-10-09 12:05:00 UTC+03:00",
      "succeeded 2026-10-09 11:05:00 UTC+03:00",
      "enabled",
    ]);
    expect(within(rows[0] as HTMLElement).getByRole("link", { name: "Feeders, hourly" }).getAttribute("href")).toBe(`/suites/${SUITE_ID}`);
    expect(within(rows[0] as HTMLElement).getByRole("link", { name: /succeeded/ }).getAttribute("href")).toBe(`/runs/${RUN_ID}`);
    expect(cells(rows[1] as HTMLElement)).toEqual(["Orphan", "dataset deleted", "Only on request", "—", "—", "disabled"]);

    expect(screen.getByRole("link", { name: "New suite" }).getAttribute("href")).toBe("/suites/new");
    expect(screen.getByRole("link", { name: "Suites" }).getAttribute("href")).toBe("/suites");
  });

  it("hides New suite from viewers", async () => {
    stubFetch(route, asRole("viewer"));
    renderApp("/suites");
    await screen.findByRole("table");
    expect(screen.queryByRole("link", { name: "New suite" })).toBeNull();
  });

  it("follows the next page and says when there are none", async () => {
    stubFetch((url) => {
      if (url === "/api/suites?limit=100") return { items: [suite()], next_cursor: "n1" };
      if (url === "/api/suites?limit=100&cursor=n1") return { items: [suite({ id: OTHER_ID, name: "Second" })], next_cursor: null };
      throw new Error(`unexpected ${url}`);
    });
    renderApp("/suites");
    expect(await screen.findByRole("link", { name: "Second" })).toBeTruthy();
  });

  it("says when there are no suites, and shows a load error", async () => {
    stubFetch(() => ({ items: [], next_cursor: null }));
    const first = renderApp("/suites");
    expect(await screen.findByText(/No suites yet/)).toBeTruthy();
    first.unmount();
    stubFetch(() => new Response(JSON.stringify({ detail: "boom" }), { status: 500 }));
    renderApp("/suites");
    expect(await screen.findByText("Could not load suites: boom")).toBeTruthy();
  });
});
