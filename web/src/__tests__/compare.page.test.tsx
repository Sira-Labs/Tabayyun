import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CompareChartResponse, ResidualResponse, SeriesGroup } from "../compare/api";
import { PALETTE } from "../compare/palette";
import type { Finding } from "../types";
import { json, renderApp, stubFetch } from "./helpers";
import { FINDING_ID, GROUP_ID, H, IDS, T0, compareSeries, finding } from "./compareFake";

type Opts = {
  height: number;
  series: { label?: string; stroke?: string; width?: number; dash?: number[]; scale?: string; show?: boolean }[];
  axes: { label?: string; side?: number; scale?: string }[];
  bands?: unknown[];
  cursor: { sync: { key: string } };
  hooks: { setSelect: ((u: unknown) => void)[]; setCursor: ((u: unknown) => void)[] };
};
type Fake = { opts: Opts; data: (number | null | undefined)[][]; setSeries: ReturnType<typeof vi.fn>; setCursor: ReturnType<typeof vi.fn>; destroyed: boolean };

// jsdom has no canvas: the uPlot wrapper is replaced, and what the page hands it is recorded.
const plots = vi.hoisted(() => [] as Fake[]);
vi.mock("../components/chart/uplot", () => ({
  createPlot: (opts: Opts, data: Fake["data"]) => {
    const fake: Fake = { opts, data, setSeries: vi.fn(), setCursor: vi.fn(), destroyed: false };
    plots.push(fake);
    return {
      series: opts.series.map((s) => ({ show: s.show ?? true })),
      setSeries: fake.setSeries,
      setCursor: fake.setCursor,
      setSize: vi.fn(),
      setData: vi.fn(),
      redraw: vi.fn(),
      setSelect: vi.fn(),
      valToPos: (v: number) => v,
      destroy: () => {
        fake.destroyed = true;
      },
      over: document.createElement("div"),
    };
  },
  // A plain outer join, as uPlot.join does.
  joinTables: (tables: [number[], (number | null)[]][]) => {
    const xs = [...new Set(tables.flatMap((t) => t[0]))].sort((a, b) => a - b);
    return [xs, ...tables.map(([tx, ty]) => xs.map((x) => (tx.includes(x) ? ty[tx.indexOf(x)] : undefined)))];
  },
  syncKey: (name: string) => name,
}));

beforeEach(() => {
  plots.length = 0;
});

const live = () => plots.filter((p) => !p.destroyed);
const END = T0 + 72 * H;

function chartOf(ids: string[], from = T0, to = END, patch: (n: number) => Partial<CompareChartResponse["series"][number]> = () => ({})): CompareChartResponse {
  return {
    bins: { from: String(from), to: String(to), width_px: 1200 },
    series: ids.map((id) => {
      const n = IDS.indexOf(id);
      return compareSeries(n, { ts: [String(from + n * 60e9), String(from + H)], ...patch(n) });
    }),
  };
}

const GROUP: SeriesGroup = {
  id: GROUP_ID,
  name: "SS-North",
  kind: "balance",
  members: [
    { series_id: IDS[1]!, external_id: "f1", role: "output" },
    { series_id: IDS[2]!, external_id: "f2", role: "output" },
    { series_id: IDS[3]!, external_id: "f3", role: "output" },
    { series_id: IDS[0]!, external_id: "in", role: "input" },
  ],
  params: {},
};

const RESIDUAL: ResidualResponse = {
  kind: "balance",
  grid_ns: H,
  window: { from: String(T0), to: String(T0 + 72 * H) },
  ts: [String(T0), String(T0 + H)],
  residual: [1, 9],
  band: { lo: [0, 0], hi: [2, 2] },
  missing: [],
};

type Backend = {
  chart?: (q: URLSearchParams) => unknown;
  findings?: (seriesId: string) => Finding[];
  group?: SeriesGroup;
  residual?: unknown;
  finding?: Finding;
};

function backend({ chart, findings = () => [], group = GROUP, residual = RESIDUAL, finding: one }: Backend = {}) {
  const asked = { charts: [] as URLSearchParams[], findings: [] as URLSearchParams[], residuals: [] as URLSearchParams[] };
  stubFetch((url) => {
    const [path = "", query = ""] = url.split("?");
    const q = new URLSearchParams(query);
    if (path === "/api/compare/chart") {
      asked.charts.push(q);
      if (chart) return chart(q);
      return q.get("from") ? chartOf(q.getAll("series"), Number(q.get("from")), Number(q.get("to"))) : chartOf(q.getAll("series"));
    }
    if (path === "/api/findings") {
      asked.findings.push(q);
      return { items: findings(q.get("series_id")!), next_cursor: null };
    }
    if (path === `/api/findings/${FINDING_ID}`) return one ?? json({ detail: "finding not found" }, 404);
    if (path === `/api/series-groups/${GROUP_ID}`) return group;
    if (path === `/api/series-groups/${GROUP_ID}/residual`) {
      asked.residuals.push(q);
      return residual;
    }
    if (path === "/api/series") {
      return { items: IDS.slice(0, 4).map((id, n) => ({ id, source_id: "s", external_id: `f${n + 1}`, name: `F${n + 1}`, unit: "MW", kind: "measurement", latest_score: null, open_findings: 0, last_run_at: null })), next_cursor: null };
    }
    if (path.startsWith("/api/series/")) {
      const n = IDS.indexOf(path.slice("/api/series/".length));
      return n === -1 ? json({ detail: "series not found" }, 404) : { ...compareSeries(n), source_id: "s", external_id: `f${n + 1}` };
    }
    throw new Error(`unexpected ${url}`);
  });
  return asked;
}

async function chartShown(count = 1) {
  await screen.findByTestId("compare-chart");
  await waitFor(() => expect(live().length).toBe(count));
}

const two = `${IDS[0]},${IDS[1]}`;

describe("Compare", () => {
  it("draws two series on one plot with one request, a legend and the findings of each", async () => {
    const own = finding({ id: "a0000000-0000-4000-8000-000000000001", series_id: IDS[1]!, check_id: "tby.spikes", evidence: {}, summary: "3 spikes" });
    const asked = backend({ findings: (id) => (id === IDS[1] ? [own] : []) });
    renderApp(`/compare?series=${two}`);
    await chartShown();
    expect(asked.charts).toHaveLength(1);
    expect(asked.charts[0]!.toString()).toBe(`series=${IDS[0]}&series=${IDS[1]}&width_px=1200`);
    const plot = live()[0]!;
    expect(plot.opts.series.slice(1).map((s) => s.label)).toEqual(["F1", "F2"]);
    expect(plot.opts.series.slice(1).map((s) => s.stroke)).toEqual([PALETTE.light[0], PALETTE.light[1]]);
    // Outer join: F2's first point is a minute later than F1's.
    expect(plot.data).toEqual([
      [T0 / 1e9, T0 / 1e9 + 60, T0 / 1e9 + 3600],
      [10, undefined, 20],
      [undefined, 10, 20],
    ]);
    expect(screen.getByTestId("compare-chart").getAttribute("aria-label")).toMatch(/^Comparison of F1 and F2, .* No finding selected\./);
    const legend = screen.getByRole("list", { name: "Series" });
    expect(within(legend).getAllByRole("button").map((b) => b.getAttribute("aria-pressed"))).toEqual(["true", "true"]);
    expect(await screen.findByText("3 spikes")).toBeTruthy();
    expect(asked.findings.map((q) => q.get("series_id")).sort()).toEqual([IDS[0], IDS[1]]);
    expect(asked.findings[0]!.get("since")).toBe(String(T0));
    expect(asked.findings[0]!.get("until")).toBe(String(END));
  });

  it("gives two units two axes on one plot and three units stacked lanes that share the cursor", async () => {
    const units = ["MW", "kV", "°C"];
    backend({ chart: (q) => chartOf(q.getAll("series"), T0, END, (n) => ({ unit: units[n]! })) });
    const first = renderApp(`/compare?series=${two}`);
    await chartShown();
    expect(live()[0]!.opts.axes.slice(1).map((a) => [a.label, a.side])).toEqual([["MW", 3], ["kV", 1]]);
    expect(live()[0]!.opts.series.slice(1).map((s) => s.scale)).toEqual(["y0", "y1"]);
    first.unmount();
    plots.length = 0;

    renderApp(`/compare?series=${two},${IDS[2]}`);
    await chartShown(3);
    expect(live().map((p) => p.opts.axes[1]!.label)).toEqual(["MW", "kV", "°C"]);
    expect(new Set(live().map((p) => p.opts.cursor.sync.key)).size).toBe(1);
  });

  it("zooms by drag through the URL, goes back, resets, and takes the window inputs", async () => {
    const asked = backend();
    const user = userEvent.setup();
    const { router } = renderApp(`/compare?series=${two}&norm=z`);
    await chartShown();
    live()[0]!.opts.hooks.setSelect[0]!({ select: { left: 10, width: 20 }, posToVal: (px: number) => T0 / 1e9 + px * 3600, setSelect: vi.fn() });
    const zoomed = { series: two, from: String(T0 + 10 * H), to: String(T0 + 30 * H), norm: "z" };
    await waitFor(() => expect(router.state.location.search).toEqual(zoomed));
    await waitFor(() => expect(asked.charts.at(-1)!.get("from")).toBe(String(T0 + 10 * H)));
    // A synced lane reporting the same drag does not zoom twice.
    live()[0]!.opts.hooks.setSelect[0]!({ select: { left: 10, width: 20 }, posToVal: (px: number) => T0 / 1e9 + px * 3600, setSelect: vi.fn() });

    await user.click(screen.getByRole("button", { name: "Back" }));
    await waitFor(() => expect(router.state.location.search).toEqual({ series: two, from: String(T0), to: String(END), norm: "z" }));
    await waitFor(() => expect((screen.getByRole("button", { name: "Back" }) as HTMLButtonElement).disabled).toBe(true));
    await user.click(screen.getByRole("button", { name: "Reset" }));
    await waitFor(() => expect(router.state.location.search).toEqual({ series: two, norm: "z" }));
    expect(screen.getByText(/z-scores use the mean and spread of the visible window/)).toBeTruthy();
  });

  it("normalises from the stats, keeps raw values in the readout and explains refusals", async () => {
    backend({
      chart: (q) =>
        chartOf(q.getAll("series"), T0, END, (n) =>
          n === 1 ? { stats: { min: 0, max: 20, mean: 10, sd: 5, first: { ts: String(T0), value: 0 } } } : n === 2 ? { stats: null } : n === 3 ? { ts: [], values: [], stats: null } : {},
        ),
    });
    const user = userEvent.setup();
    renderApp(`/compare?series=${IDS.slice(0, 4).join(",")}&norm=index`);
    await chartShown();
    const plot = live()[0]!;
    expect(plot.opts.series.slice(1).map((s) => s.label)).toEqual(["F1"]);
    expect(plot.data[1]).toEqual([100, 200]);
    expect(plot.opts.axes[1]!.label).toBe("Index (first value = 100)");
    const legend = screen.getByRole("list", { name: "Series" });
    expect(within(legend).getByRole("button", { name: /F2/ }).textContent).toMatch(/index is unavailable: the first value is zero or negative/);
    expect(within(legend).getByRole("button", { name: /F3/ }).textContent).toMatch(/no usable samples in the window/);
    expect(within(legend).getByRole("button", { name: /F4/ }).textContent).toMatch(/no data/);

    // The keyboard cursor: bin by bin, raw values with units (also of the series the mode
    // cannot draw), announced.
    const figure = screen.getByTestId("compare-chart");
    figure.focus();
    await user.keyboard("{ArrowRight}");
    await waitFor(() => expect(screen.getByTestId("compare-live").textContent).toMatch(/: F1 10 MW; F2 10 MW; F3 10 MW; F4 no value$/));
    expect(within(screen.getByTestId("compare-readout")).getAllByText("10 MW")).toHaveLength(3);
    expect(live()[0]!.setCursor).toHaveBeenCalled();
    await user.keyboard("{ArrowRight}");
    await waitFor(() => expect(screen.getByTestId("compare-live").textContent).toMatch(/: F1 20 MW; F2 20 MW; F3 20 MW; F4 no value$/));

    // A legend button hides its line.
    await user.click(within(legend).getByRole("button", { name: /F1/ }));
    expect(within(legend).getByRole("button", { name: /F1/ }).getAttribute("aria-pressed")).toBe("false");
    expect(live()[0]!.setSeries).toHaveBeenCalledWith(1, { show: false });
  });

  it("opens a balance finding: members by role, the suspect emphasised, the residual lane and the summary", async () => {
    const f = finding();
    const asked = backend({ finding: f, findings: (id) => (id === f.series_id ? [f] : []) });
    const { router } = renderApp(`/compare?series=${IDS.slice(0, 4).join(",")}&group=${GROUP_ID}&finding=${FINDING_ID}`);
    await chartShown(2);
    // Without a window in the URL, the request is centred on the finding.
    expect(asked.charts[0]!.get("from")).toBe(String(T0 + 28.5 * H));
    expect(asked.charts[0]!.get("to")).toBe(String(T0 + 37.5 * H));
    expect(router.state.location.search).toEqual({ series: IDS.slice(0, 4).join(","), group: GROUP_ID, finding: FINDING_ID });
    await waitFor(() => expect(live()[0]!.opts.series[3]!.width).toBe(3));
    const [main, lane] = live();
    // The input first, then the outputs; F3 is the suspect.
    expect(main!.opts.series.slice(1).map((s) => s.label)).toEqual(["F1", "F2", "F3", "F4"]);
    expect(main!.opts.series[3]!.stroke).toBe(PALETTE.light[2]);
    expect(main!.opts.series[1]!.stroke).toMatch(/^rgba\(.*, 0\.4\)$/);
    expect(lane!.opts.series.slice(1).map((s) => s.label)).toEqual(["inputs − outputs", "loss band, upper", "loss band, lower"]);
    expect(lane!.opts.bands).toHaveLength(1);
    expect(asked.residuals[0]!.get("from")).toBe(String(T0 + 28.5 * H));
    expect(screen.getByRole("status").textContent).toMatch(/Selected finding: Balance SS-North does not close for 6h; F3 explains most of it \(suspect: F3\)/);
    expect(within(screen.getByRole("list", { name: "Series" })).getByRole("button", { name: /F3/ }).textContent).toMatch(/suspect/);
    expect(screen.getByTestId("compare-chart").getAttribute("aria-label")).toMatch(/Selected finding: Balance SS-North/);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Compare F1, F2, F3 and F4 (SS-North)");
  });

  it("names a missing member instead of drawing a residual, and selects a finding from the list", async () => {
    const f = finding();
    backend({ residual: { ...RESIDUAL, ts: [], residual: [], band: { lo: [], hi: [] }, missing: [IDS[3]] }, finding: f, findings: (id) => (id === f.series_id ? [f] : []) });
    const user = userEvent.setup();
    const { router } = renderApp(`/compare?series=${IDS.slice(0, 4).join(",")}&group=${GROUP_ID}`);
    await chartShown(1);
    expect(await screen.findByText("No residual: F4 has no usable samples in this window.")).toBeTruthy();
    await user.click(await screen.findByRole("button", { name: /Balance SS-North does not close/ }));
    await waitFor(() =>
      expect(router.state.location.search).toEqual({
        series: IDS.slice(0, 4).join(","),
        from: String(T0 + 28.5 * H),
        to: String(T0 + 37.5 * H),
        group: GROUP_ID,
        finding: FINDING_ID,
      }),
    );
    await waitFor(() => expect(screen.getByRole("button", { name: /Balance SS-North does not close/ }).getAttribute("aria-current")).toBe("true"));
  });

  it("shows the table view with the findings", async () => {
    const f = finding({ window: { start: T0 + H, end: T0 + 2 * H } });
    backend({ findings: (id) => (id === IDS[0] ? [f] : []) });
    const user = userEvent.setup();
    renderApp(`/compare?series=${two}`);
    await chartShown();
    await user.click(screen.getByRole("button", { name: "View as table" }));
    const table = screen.getByRole("table", { name: "Minimum and maximum of each series per chart bin" });
    // Two bins hold points: the first (both series) and the one an hour in.
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(within(table).getByRole("columnheader", { name: "F1 (MW), min and max" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Export the table as CSV" })).toBeTruthy();
    const findingsTable = await screen.findByRole("table", { name: "Findings in this window (1)" });
    expect(within(findingsTable).getByText("tby.balance_residual")).toBeTruthy();
  });

  it("asks for 2 to 8 series with a picker, and lists unknown ids", async () => {
    backend({ chart: () => json({ detail: { message: "series not found", unknown: [IDS[8]] } }, 404) });
    const user = userEvent.setup();
    const first = renderApp(`/compare?series=${IDS[0]}`);
    expect((await screen.findByRole("alert")).textContent).toMatch(/Choose at least 2 series to compare; one is chosen\./);
    await user.click(await screen.findByRole("checkbox", { name: /F2/ }));
    await user.click(screen.getByRole("button", { name: "Compare" }));
    await waitFor(() => expect(first.router.state.location.search).toEqual({ series: two }));
    first.unmount();

    const many = renderApp(`/compare?series=${IDS.join(",")}`);
    expect((await screen.findByRole("alert")).textContent).toMatch(/At most 8 series can be compared; 9 are chosen\./);
    expect((screen.getByRole("button", { name: "Compare" }) as HTMLButtonElement).disabled).toBe(true);
    many.unmount();

    renderApp(`/compare?series=${IDS[0]},${IDS[8]}`);
    await waitFor(() => expect(screen.getByRole("alert").textContent).toMatch(new RegExp(`These series are unknown:${IDS[8]}`)));
    expect(await screen.findByRole("button", { name: "Remove F1" })).toBeTruthy();
  });

  it("links to the compare view from a series page and from a cross-series finding", async () => {
    const f = finding({ series_id: IDS[0]! });
    stubFetch((url) => {
      const [path] = url.split("?");
      if (path === `/api/series/${IDS[0]}`) return { ...compareSeries(0), source_id: "src", external_id: "f1", kind: "measurement", operational_min: null, operational_max: null, latest_score: null, open_findings: 1, n_runs: 1, last_run_at: null };
      if (path === "/api/sources/src") return { id: "src", name: "PI", type: "upload" };
      if (path === `/api/series/${IDS[0]}/chart`) return { window: { from: String(T0), to: String(END) }, extent: { start: String(T0), end: String(END) }, layer: "raw", width_px: 1200, n_raw: 2, ts: [String(T0)], values: [1], quality: [] };
      if (path === `/api/series/${IDS[0]}/profile`) return { window: null, profile: null, quality_counts: { good: 0, uncertain: 0, bad: 0, estimated: 0 }, band: null };
      if (path === "/api/findings") return { items: [f], next_cursor: null };
      if (path === "/api/series") return { items: IDS.slice(0, 3).map((id, n) => ({ id, source_id: "src", external_id: `f${n + 1}`, name: `F${n + 1}`, unit: "MW", kind: "measurement", latest_score: null, open_findings: 0, last_run_at: null })), next_cursor: null };
      throw new Error(`unexpected ${url}`);
    });
    const user = userEvent.setup();
    const { router } = renderApp(`/series/${IDS[0]}`);
    const link = await screen.findByRole("link", { name: "Compare the series involved" });
    const href = new URL(link.getAttribute("href")!, "http://x");
    expect(href.pathname).toBe("/compare");
    expect(href.searchParams.get("series")).toBe(IDS.slice(0, 4).join(","));
    expect(href.searchParams.get("group")).toBe(GROUP_ID);
    expect(href.searchParams.get("finding")).toBe(FINDING_ID);

    await user.click(screen.getByRole("button", { name: "Compare with…" }));
    // The page's own series is not offered.
    expect(await screen.findByRole("checkbox", { name: /F2/ })).toBeTruthy();
    expect(screen.queryByRole("checkbox", { name: /F1/ })).toBeNull();
    await user.click(screen.getByRole("checkbox", { name: /F3/ }));
    await user.click(screen.getByRole("button", { name: "Compare" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/compare"));
    expect(router.state.location.search).toEqual({ series: `${IDS[0]},${IDS[2]}` });
  });
});
