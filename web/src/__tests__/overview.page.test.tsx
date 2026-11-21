import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { renderApp } from "./helpers";
import { OPC_ID, PI_ID, SERIES_A, SERIES_B, emptyOverview, overview, overviewBackend, worstItem } from "./overviewFake";

const cells = (row: HTMLElement) => within(row).getAllByRole("cell").map((c) => c.textContent);
const params = (href: string | null) => Object.fromEntries(new URLSearchParams((href ?? "").split("?")[1]));
const pathOf = (href: string | null) => (href ?? "").split("?")[0];

describe("overview.page", () => {
  it("is the start page, first in the navigation, active only on /", async () => {
    overviewBackend();
    renderApp("/");
    expect(await screen.findByRole("heading", { level: 1, name: "Overview" })).toBeTruthy();
    const nav = screen.getByRole("navigation", { name: "Main" });
    const links = within(nav).getAllByRole("link");
    expect(links[0]?.textContent).toBe("Overview");
    expect(links[0]?.getAttribute("href")).toBe("/");
    expect(links[0]?.getAttribute("aria-current")).toBe("page");
    expect(within(nav).getByRole("link", { name: "Runs" }).getAttribute("aria-current")).toBeNull();
  });

  it("shows eight tiles in order with mean, lowest, series count and the sparkline's text", async () => {
    overviewBackend();
    renderApp("/");
    const scores = await screen.findByRole("region", { name: "Scores" });
    const tiles = within(scores).getAllByRole("listitem");
    expect(tiles.map((t) => within(t).getByRole("heading").textContent)).toEqual([
      "Overall",
      "Completeness",
      "Timeliness",
      "Validity",
      "Accuracy",
      "Consistency",
      "Plausibility",
      "Integrity",
    ]);
    const overall = tiles[0] as HTMLElement;
    expect(overall.textContent).toContain("96.4");
    expect(overall.textContent).toContain("lowest 41.0 · 9,950 series");
    expect(within(overall).getByRole("img", { name: "from 97.1 to 96.4 over 30 days" })).toBeTruthy();
    expect(within(overall).getByRole("img").querySelector("polyline")?.getAttribute("points")?.split(" ")).toHaveLength(3);
    // The mean is coloured like the score tiles: poor below 70, fair below 90.
    const completeness = tiles[1] as HTMLElement;
    expect(within(completeness).getByText("65.5").className).toContain("text-red-700");
    expect(within(overall).getByText("96.4", { selector: "p" }).className).toContain("text-emerald-700");
  });

  it("lists the worst series with links to the series and to its unresolved findings", async () => {
    overviewBackend();
    renderApp("/");
    const table = await screen.findByRole("table", { name: "Worst series, lowest value first" });
    const rows = within(table).getAllByRole("row").slice(1) as HTMLElement[];
    expect(cells(rows[0] as HTMLElement)).toEqual(["Feeder 4 pressure", "plant/feeder-4", "Completeness", "41.0", "88.2", "12"]);
    expect(cells(rows[1] as HTMLElement)).toEqual(["Feeder 5 flow", "—", "Timeliness", "55.5", "90.0", "0"]);
    const name = within(rows[0] as HTMLElement).getByRole("link", { name: "Feeder 4 pressure" });
    expect(name.getAttribute("href")).toBe(`/series/${SERIES_A}`);
    const findings = within(rows[0] as HTMLElement).getByRole("link", { name: "12 unresolved findings of Feeder 4 pressure" });
    expect(pathOf(findings.getAttribute("href"))).toBe("/findings");
    expect(params(findings.getAttribute("href"))).toEqual({ group: "series", open: SERIES_A });
    // No findings, no link.
    expect(within(rows[1] as HTMLElement).queryAllByRole("link").map((a) => a.getAttribute("href"))).toEqual([`/series/${SERIES_B}`]);
    expect(screen.getByText("2 of 498 shown")).toBeTruthy();
  });

  it("does not say how many are shown when the list holds them all", async () => {
    overviewBackend(() => overview({ worst: { share: 0.05, n: 1, items: [worstItem()] } }));
    renderApp("/");
    await screen.findByRole("table", { name: "Worst series, lowest value first" });
    expect(screen.queryByText(/ of \d+ shown/)).toBeNull();
  });

  it("draws findings per day with a table alternative and links the unresolved counts", async () => {
    overviewBackend();
    renderApp("/");
    const section = await screen.findByRole("region", { name: "New findings per day" });
    expect(
      within(section).getByRole("img", { name: "New findings per day from 2026-10-23 to 2026-11-21: 12 in total, most on 2026-11-20 (7)" }),
    ).toBeTruthy();
    // Every day has its slot; the two days with findings have bars.
    const svg = within(section).getByRole("img");
    expect(svg.querySelectorAll("g")).toHaveLength(30);
    expect(svg.querySelectorAll("g title")[29]?.textContent).toBe("2026-11-21: 1 critical, 3 high, 1 low");
    expect(svg.querySelectorAll("rect.fill-red-700")).toHaveLength(1);

    const table = within(section).getByRole("table", { name: "New findings per day by severity" });
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(31);
    expect(within(rows[0] as HTMLElement).getAllByRole("columnheader").map((c) => c.textContent)).toEqual([
      "Day",
      "critical",
      "high",
      "medium",
      "low",
      "Total",
    ]);
    expect(within(rows.at(-2) as HTMLElement).getByRole("rowheader").textContent).toBe("2026-11-20");
    expect(cells(rows.at(-2) as HTMLElement)).toEqual(["0", "2", "5", "0", "7"]);
    expect(cells(rows.at(-1) as HTMLElement)).toEqual(["1", "3", "0", "1", "5"]);

    const unresolved = within(section).getByRole("list", { name: "Unresolved findings by severity" });
    const links = within(unresolved).getAllByRole("link");
    expect(links.map((a) => a.getAttribute("aria-label"))).toEqual(["1 unresolved critical", "14 unresolved high", "40 unresolved medium", "3 unresolved low"]);
    expect(pathOf(links[1]?.getAttribute("href") ?? null)).toBe("/findings");
    expect(params(links[1]?.getAttribute("href") ?? null)).toEqual({ severity: "high" });
  });

  it("shows a chip per source with health and series count, the disabled one marked", async () => {
    overviewBackend();
    renderApp("/");
    const list = await screen.findByRole("list", { name: "Sources and their health" });
    const chips = within(list).getAllByRole("link");
    expect(chips.map((c) => c.getAttribute("href"))).toEqual([`/sources/${PI_ID}`, `/sources/${OPC_ID}`]);
    expect(chips[0]?.textContent).toBe("Historianok4,200 series");
    expect(chips[1]?.textContent).toBe("Old OPCfailing12 seriesdisabled");
    expect(chips[0]?.getAttribute("title")).toBe("PI Web API · last fetch 2026-11-21 08:00:00 UTC+03:00");
    expect(chips[1]?.getAttribute("title")).toBe("OPC UA · never fetched");
  });

  it("reads a last fetch sent as the stored health's object", async () => {
    const base = overview();
    const source = { ...(base.sources[0] as (typeof base.sources)[number]), last_fetch: { finished_at: "2026-11-21T05:00:00Z" } };
    overviewBackend(() => ({ ...base, sources: [source] }));
    renderApp("/");
    const list = await screen.findByRole("list", { name: "Sources and their health" });
    expect(within(list).getByRole("link").getAttribute("title")).toBe("PI Web API · last fetch 2026-11-21 08:00:00 UTC+03:00");
  });

  it("says nothing was scored yet in an empty workspace, with a link to a new run", async () => {
    overviewBackend(() => emptyOverview());
    renderApp("/");
    expect(await screen.findByText(/Nothing was scored in this workspace yet/)).toBeTruthy();
    const main = screen.getByRole("main");
    expect(within(main).getByRole("link", { name: "New run" }).getAttribute("href")).toBe("/runs/new");
    expect(screen.queryByRole("region", { name: "Scores" })).toBeNull();
    expect(screen.getByText("No sources yet.")).toBeTruthy();
  });

  it("shows the parts with zeros when a filter matches nothing", async () => {
    overviewBackend(() => emptyOverview());
    renderApp("/?asset=nowhere/");
    const scores = await screen.findByRole("region", { name: "Scores" });
    expect(within(scores).getAllByRole("img", { name: "no scores in the last 30 days" })).toHaveLength(8);
    expect(screen.getByText("No scored series.")).toBeTruthy();
    expect(screen.queryByText(/Nothing was scored/)).toBeNull();
  });

  it("says when the overview could not be loaded", async () => {
    overviewBackend(() => new Response(JSON.stringify({ detail: "boom" }), { status: 500 }));
    renderApp("/");
    expect((await screen.findByRole("alert")).textContent).toContain("Could not load the overview");
  });
});
