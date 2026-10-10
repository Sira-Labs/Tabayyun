import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ChartResponse, ProfileResponse } from "../seriesChart";
import type { Finding, Series } from "../types";
import { json, renderApp, stubFetch } from "./helpers";
import { PI_ID, detail } from "./sourcesFake";

// jsdom has no canvas: the uPlot wrapper is replaced, and what the page hands it is recorded.
const plots = vi.hoisted(() => [] as { opts: Record<string, unknown>; data: unknown; setData: ReturnType<typeof vi.fn> }[]);
vi.mock("../components/chart/uplot", () => ({
  createPlot: (opts: Record<string, unknown>, data: unknown) => {
    const setData = vi.fn();
    plots.push({ opts, data, setData });
    return { setData, redraw: vi.fn(), setSize: vi.fn(), destroy: vi.fn(), setSelect: vi.fn(), over: document.createElement("div") };
  },
}));

beforeEach(() => {
  plots.length = 0;
});

const SID = "5e7a1c2b-0000-4000-8000-000000000001";
const H = 3600e9;
const T0 = 1_767_225_600_000_000_000;
const END = T0 + 72 * H;
const FINDING_ID = "f0000000-0000-4000-8000-000000000001";

function series(overrides: Partial<Series> = {}): Series {
  return {
    id: SID,
    source_id: PI_ID,
    external_id: "\\\\PISRV01\\FIC101.PV",
    name: "FIC101",
    unit: "m3/h",
    kind: "measurement",
    physical_min: 0,
    physical_max: 200,
    operational_min: null,
    operational_max: null,
    resolution: 0.01,
    non_negative: true,
    asset_path: "Plant\\Unit 1",
    expected_interval_ns: H,
    metadata: { compressing: true },
    latest_score: { overall: 82.5, computed_at: "2026-01-04T00:00:00Z" },
    open_findings: 1,
    n_runs: 3,
    last_run_at: "2026-01-04T00:00:00Z",
    ...overrides,
  };
}

function chart(from = T0, to = END, overrides: Partial<ChartResponse> = {}): ChartResponse {
  return {
    window: { from: String(from), to: String(to) },
    extent: { start: String(T0), end: String(END) },
    layer: "raw",
    width_px: 1200,
    n_raw: 72,
    ts: [String(from), String(from + H)],
    values: [50, 51],
    quality: [{ start: String(T0 + 10 * H), end: String(T0 + 20 * H), quality: "bad" }],
    ...overrides,
  };
}

const PROFILE: ProfileResponse = {
  window: { from: String(T0), to: String(END) },
  profile: {
    n_samples: 62,
    n_finite: 62,
    expected_interval_ns: H,
    median: 50,
    mad: 5,
    p001: 40.2,
    p999: 59.8,
    min: 40,
    max: 60,
    resolution: 1,
    noise_mad: 0.7,
    dominant_period_ns: 24 * H,
    seasonal_strength: 0.81,
  },
  quality_counts: { good: 62, uncertain: 0, bad: 10, estimated: 0 },
  band: { lo: 35.2, hi: 64.8, source: "profile" },
};

const FINDING: Finding = {
  id: FINDING_ID,
  check_id: "tby.spikes",
  series_id: SID,
  dimension: "plausibility",
  severity: "high",
  window: { start: T0 + 30 * H, end: T0 + 34 * H },
  score_impact: 1,
  summary: "3 spikes up to 180 m3/h",
  evidence: {},
  status: "open",
  status_reason: null,
  first_run_id: null,
  last_run_id: null,
  occurrences: 1,
  created_at: "",
  updated_at: "",
};

type Asked = { charts: URLSearchParams[]; findings: URLSearchParams[] };

/** A fake backend; `chartFor` answers each chart request. */
function backend(chartFor: (q: URLSearchParams) => unknown = (q) => (q.get("from") ? chart(Number(q.get("from")), Number(q.get("to"))) : chart())) {
  const asked: Asked = { charts: [], findings: [] };
  stubFetch((url) => {
    const [path, query = ""] = url.split("?");
    const q = new URLSearchParams(query);
    if (path === `/api/series/${SID}`) return series();
    if (path === `/api/sources/${PI_ID}`) return detail();
    if (path === `/api/series/${SID}/chart`) {
      asked.charts.push(q);
      return chartFor(q);
    }
    if (path === `/api/series/${SID}/profile`) return PROFILE;
    if (path === "/api/findings") {
      asked.findings.push(q);
      return { items: [FINDING], next_cursor: null };
    }
    throw new Error(`unexpected ${url}`);
  });
  return asked;
}

async function chartShown() {
  await screen.findByTestId("series-chart");
  await waitFor(() => expect(plots.length).toBeGreaterThan(0));
}

describe("SeriesDetail", () => {
  it("shows the header, chart, rug, findings, metadata and profile", async () => {
    const asked = backend();
    renderApp(`/series/${SID}`);
    expect(await screen.findByRole("heading", { name: "FIC101" })).toBeTruthy();
    await chartShown();
    expect(asked.charts[0]!.toString()).toBe("width_px=1200");
    expect(plots[0]!.data).toEqual([[T0 / 1e9, T0 / 1e9 + 3600], [50, 51]]);
    expect(screen.getByRole("link", { name: "PI plant" }).getAttribute("href")).toBe(`/sources/${PI_ID}`);
    const rug = screen.getByRole("list", { name: "Quality" });
    expect(within(rug).getAllByRole("listitem")).toHaveLength(1);
    expect(within(rug).getByRole("listitem").getAttribute("aria-label")).toMatch(/^bad quality from /);
    expect(await screen.findByText("3 spikes up to 180 m3/h")).toBeTruthy();
    expect(asked.findings[0]!.get("since")).toBe(String(T0));
    expect(asked.findings[0]!.get("until")).toBe(String(END));
    expect(screen.getByText(/learned from the last 28 days/)).toBeTruthy();
    const meta = screen.getByRole("region", { name: "Metadata" });
    expect(within(meta).getByText("Plant\\Unit 1")).toBeTruthy();
    expect(within(meta).getByText("compressing")).toBeTruthy();
    const profile = screen.getByRole("region", { name: "Profile" });
    expect(within(profile).getByText("40.2 to 59.8")).toBeTruthy();
    expect(within(profile).getByText(/good 86.1 %, bad 13.9 %/)).toBeTruthy();
  });

  it("changes the range with presets, keys, drag and back, through the URL", async () => {
    const asked = backend();
    const user = userEvent.setup();
    const { router } = renderApp(`/series/${SID}`);
    await chartShown();

    await user.click(screen.getByRole("button", { name: "1 d" }));
    await waitFor(() => expect(router.state.location.search).toEqual({ from: String(END - 24 * H), to: String(END) }));
    await waitFor(() => expect(asked.charts.at(-1)!.get("from")).toBe(String(END - 24 * H)));

    const figure = screen.getByTestId("series-chart");
    figure.focus();
    await user.keyboard("{ArrowLeft}");
    await waitFor(() => expect(router.state.location.search).toEqual({ from: String(END - 36 * H), to: String(END - 12 * H) }));
    await user.keyboard("+");
    await waitFor(() => expect(router.state.location.search).toEqual({ from: String(END - 30 * H), to: String(END - 18 * H) }));
    await user.keyboard("{Home}");
    await waitFor(() => expect(router.state.location.search).toEqual({ from: String(T0), to: String(END) }));

    // A drag of the plot area: uPlot's setSelect hook with the dragged pixels.
    const setSelect = (plots[0]!.opts.hooks as { setSelect: ((u: unknown) => void)[] }).setSelect[0]!;
    setSelect({ select: { left: 100, width: 50 }, posToVal: (px: number) => T0 / 1e9 + px * 3600, setSelect: vi.fn() });
    await waitFor(() => expect(router.state.location.search).toEqual({ from: String(T0 + 100 * H), to: String(T0 + 150 * H) }));

    await user.click(screen.getByRole("button", { name: "Back" }));
    await waitFor(() => expect(router.state.location.search).toEqual({ from: String(T0), to: String(END) }));
  });

  it("zooms to a finding and selects it", async () => {
    backend();
    const user = userEvent.setup();
    const { router } = renderApp(`/series/${SID}`);
    await user.click(await screen.findByRole("button", { name: /3 spikes up to 180/ }));
    await waitFor(() =>
      expect(router.state.location.search).toEqual({ from: String(T0 + 29 * H), to: String(T0 + 35 * H), finding: FINDING_ID }),
    );
    await waitFor(() => expect(screen.getByRole("button", { name: /3 spikes up to 180/ }).getAttribute("aria-current")).toBe("true"));
  });

  it("checks the From and To inputs", async () => {
    backend();
    const user = userEvent.setup();
    const { router } = renderApp(`/series/${SID}`);
    const form = await screen.findByRole("form", { name: "Range" });
    fireEvent.change(within(form).getByLabelText("From"), { target: { value: "2026-01-02T12:00" } });
    fireEvent.change(within(form).getByLabelText("To"), { target: { value: "2026-01-02T06:00" } });
    await user.click(within(form).getByRole("button", { name: "Show" }));
    expect(within(form).getByRole("alert").textContent).toBe("To must be later than From.");
    fireEvent.change(within(form).getByLabelText("To"), { target: { value: "2026-01-02T18:00" } });
    await user.click(within(form).getByRole("button", { name: "Show" }));
    const from = Date.parse("2026-01-02T12:00") * 1e6;
    await waitFor(() => expect(router.state.location.search).toEqual({ from: String(from), to: String(from + 6 * H) }));
  });

  it("says when nothing is cached and where to fetch", async () => {
    backend(() => chart(T0, END, { window: null, extent: null, ts: [], values: [], quality: [], n_raw: 0 }));
    renderApp(`/series/${SID}`);
    expect(await screen.findByText("No data is cached for this series yet.")).toBeTruthy();
    expect((await screen.findByRole("link", { name: "its source's page" })).getAttribute("href")).toBe(`/sources/${PI_ID}`);
  });

  it("asks to zoom in when the range is too large", async () => {
    backend(() => json({ detail: "window_too_large" }, 422));
    renderApp(`/series/${SID}?from=${T0}&to=${END}`);
    expect((await screen.findByRole("alert")).textContent).toMatch(/too many samples to draw/);
  });
});
