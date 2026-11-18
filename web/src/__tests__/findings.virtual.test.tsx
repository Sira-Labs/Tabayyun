import { RouterProvider, createMemoryHistory, createRootRoute, createRoute, createRouter } from "@tanstack/react-router";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { FindingsTable, ROW_HEIGHT, type TableSelection } from "../pages/findings/FindingsTable";
import { fakeLayout, finding } from "./findingsFake";

const N = 10_000;
const VIEWPORT = 480;
const ITEMS = Array.from({ length: N }, (_, i) => finding(i));

/** The table alone, in a router that knows the series page its rows link to. */
async function renderTable(props: Partial<Parameters<typeof FindingsTable>[0]> = {}) {
  const onLoadMore = vi.fn();
  const root = createRootRoute({
    component: () => <FindingsTable items={ITEMS} label="Findings" hasMore={false} autoLoad onLoadMore={onLoadMore} selection={null} {...props} />,
  });
  const index = createRoute({ getParentRoute: () => root, path: "/" });
  const series = createRoute({ getParentRoute: () => root, path: "/series/$seriesId" });
  const router = createRouter({ routeTree: root.addChildren([index, series]), history: createMemoryHistory({ initialEntries: ["/"] }) });
  render(<RouterProvider router={router as never} />);
  const table = await screen.findByRole("table", { name: "Findings" });
  return { table, onLoadMore, scroller: table.parentElement as HTMLElement };
}

const dataRows = (table: HTMLElement) => within(table).getAllByRole("row").slice(1);
const indexes = (table: HTMLElement) => dataRows(table).map((row) => Number(row.getAttribute("aria-rowindex")));

describe("findings.virtual", () => {
  it("draws only the rows near the visible part of 10 000 findings", async () => {
    fakeLayout(VIEWPORT);
    const { table, scroller } = await renderTable();
    // The header is row 1; every loaded finding counts, drawn or not.
    expect(table.getAttribute("aria-rowcount")).toBe(String(N + 1));
    const visible = Math.ceil(VIEWPORT / ROW_HEIGHT);
    const drawn = dataRows(table);
    expect(drawn.length).toBeGreaterThanOrEqual(visible);
    expect(drawn.length).toBeLessThanOrEqual(visible + 2 * 8 + 2);
    expect(indexes(table)[0]).toBe(2);
    expect(within(drawn[0] as HTMLElement).getByRole("link").textContent).toBe("Feeder 4 pressure");
    // The list is as high as all its rows, so the scrollbar is that of the whole list.
    expect((within(table).getAllByRole("rowgroup")[1] as HTMLElement).style.height).toBe(`${N * ROW_HEIGHT}px`);

    // Scrolled into the middle, other rows are drawn, and still only a screenful.
    await act(async () => {
      scroller.scrollTop = 5000 * ROW_HEIGHT;
      fireEvent.scroll(scroller);
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    const after = indexes(table);
    expect(after.length).toBeLessThanOrEqual(visible + 2 * 8 + 2);
    expect(after[0]).toBeGreaterThan(4980);
    expect(after[after.length - 1]).toBeLessThan(5030);
    expect(after).toEqual(after.map((_, i) => (after[0] as number) + i));
    expect(screen.getByText("Spike number 5000")).toBeTruthy();
    expect(screen.queryByText("Spike number 0")).toBeNull();
  });

  it("selects rows that are drawn and keeps the selection of those that are not", async () => {
    fakeLayout(VIEWPORT);
    const selected = new Set(["f-0", "f-9999"]);
    const selection: TableSelection = { selected, onToggle: vi.fn(), onToggleAll: vi.fn() };
    const { table } = await renderTable({ selection });
    const all = within(table).getByRole("checkbox", { name: "Select all loaded findings" }) as HTMLInputElement;
    expect(all.indeterminate).toBe(true);
    const first = dataRows(table)[0] as HTMLElement;
    expect(first.getAttribute("aria-selected")).toBe("true");
    fireEvent.click(within(dataRows(table)[1] as HTMLElement).getByRole("checkbox"));
    expect(selection.onToggle).toHaveBeenCalledWith("f-1");
    fireEvent.click(all);
    expect(selection.onToggleAll).toHaveBeenCalledTimes(1);
  });

  it("asks for the next page only near the end, and not while it may not", async () => {
    fakeLayout(VIEWPORT);
    const far = await renderTable({ hasMore: true });
    expect(far.table.getAttribute("aria-rowcount")).toBe("-1");
    expect(far.onLoadMore).not.toHaveBeenCalled();
    await act(async () => {
      far.scroller.scrollTop = (N - 12) * ROW_HEIGHT;
      fireEvent.scroll(far.scroller);
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    expect(far.onLoadMore).toHaveBeenCalled();
  });

  it("does not ask while a page loads or after one failed", async () => {
    fakeLayout(VIEWPORT);
    const { onLoadMore } = await renderTable({ items: ITEMS.slice(0, 20), hasMore: true, autoLoad: false });
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(onLoadMore).not.toHaveBeenCalled();
  });
});
