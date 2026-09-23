import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { makeRun, renderApp, stubFetch } from "./helpers";

/** Run number `n` of a list page. */
function run(n: number) {
  return makeRun({ id: `00000000-0000-0000-0000-00000000000${n}`, series: [{ id: "s", external_id: `series-${n}`, score: 90 + n }] });
}

describe("RunsList", () => {
  it("redirects / to /runs and appends the next page on load more", async () => {
    stubFetch((url) => {
      if (url === "/api/runs?limit=25") return { items: [run(1), run(2)], next_cursor: "c1" };
      if (url === "/api/runs?cursor=c1&limit=25") return { items: [run(3)], next_cursor: null };
      throw new Error(`unexpected ${url}`);
    });
    const user = userEvent.setup();
    const { router } = renderApp("/");

    const table = await screen.findByRole("table");
    expect(router.state.location.pathname).toBe("/runs");
    expect(within(table).getAllByRole("link").map((a) => a.textContent)).toEqual(["series-1", "series-2"]);

    await user.click(screen.getByRole("button", { name: "Load more" }));
    await screen.findByText("series-3");
    expect(within(table).getAllByRole("link").map((a) => a.textContent)).toEqual(["series-1", "series-2", "series-3"]);
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
    expect(within(table).getAllByRole("link")[0]?.getAttribute("href")).toBe(`/runs/${run(1).id}`);
  });
});
