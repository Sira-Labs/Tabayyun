import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { SeriesSummary } from "../types";
import { renderApp, stubFetch } from "./helpers";
import { PI_ID, summary } from "./sourcesFake";

function row(n: number, overrides: Partial<SeriesSummary> = {}): SeriesSummary {
  return {
    id: `s${n}`,
    source_id: PI_ID,
    external_id: `\\\\PISRV01\\TAG${n}`,
    name: `TAG${n}`,
    unit: "m3/h",
    kind: "measurement",
    latest_score: { overall: 55.5, computed_at: "2026-10-09T00:00:00Z" },
    open_findings: n,
    last_run_at: "2026-10-09T00:00:00Z",
    ...overrides,
  };
}

describe("SeriesCatalogue", () => {
  it("reads filters from the URL, writes them back, and pages on", async () => {
    const asked: string[] = [];
    stubFetch((url) => {
      if (url === "/api/sources") return { items: [summary()] };
      if (url === "/api/series/units") return { items: [{ unit: "m3/h", n: 2 }, { unit: "m", n: 1 }] };
      if (url.startsWith("/api/series?")) {
        asked.push(url);
        if (url.includes("cursor=c1")) return { items: [row(3)], next_cursor: null };
        return { items: [row(1), row(2, { latest_score: null, unit: null })], next_cursor: "c1" };
      }
      throw new Error(`unexpected ${url}`);
    });
    const user = userEvent.setup();
    const { router } = renderApp("/series?unit=m3%2Fh&score=60");
    const table = await screen.findByRole("table");
    expect(asked[0]).toBe("/api/series?unit=m3%2Fh&score_max=60&limit=50");
    const cells = within(table).getAllByRole("row")[1] as HTMLElement;
    expect(within(cells).getByText("PI plant")).toBeTruthy();
    expect(within(cells).getByText("55.5")).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Load more" }));
    await screen.findByText("TAG3");
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();

    await user.selectOptions(screen.getByLabelText("Source"), PI_ID);
    await waitFor(() => expect(router.state.location.search).toEqual({ unit: "m3/h", score: 60, source: PI_ID }));
    await user.type(screen.getByLabelText("Search"), "tag");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(asked.at(-1)).toBe(`/api/series?q=tag&source_id=${PI_ID}&unit=m3%2Fh&score_max=60&limit=50`));
    await user.selectOptions(screen.getByLabelText("Score"), "");
    await waitFor(() => expect(router.state.location.search).toEqual({ unit: "m3/h", source: PI_ID, q: "tag" }));
    expect(screen.getByRole("link", { name: "Series" })).toBeTruthy();
  });

  it("drops unknown filter values from the URL", async () => {
    const asked: string[] = [];
    stubFetch((url) => {
      if (url === "/api/sources") return { items: [] };
      if (url === "/api/series/units") return { items: [] };
      asked.push(url);
      return { items: [], next_cursor: null };
    });
    renderApp("/series?kind=nonsense&score=55&q=%20%20");
    expect(await screen.findByText("No series match.")).toBeTruthy();
    expect(asked[0]).toBe("/api/series?limit=50");
  });
});
