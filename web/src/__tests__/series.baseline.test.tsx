import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { formatLocalTime } from "../format";
import { BaselinePanel, baselinePolling, changeText, excludedText } from "../pages/series/BaselinePanel";
import type { Baseline } from "../seriesChart";
import { json, stubFetch } from "./helpers";
import { asRole } from "./sourcesFake";

const SID = "5e7a1c2b-0000-4000-8000-000000000001";
const URL = `/api/series/${SID}/baselines`;
const DAY = 86_400e9;
const T0 = 1_760_572_800_000_000_000;

function baseline(overrides: Partial<Baseline> = {}): Baseline {
  return {
    version: 7,
    created_at: "2026-11-17T02:37:04Z",
    trigger: "schedule",
    window: { from: String(T0), to: String(T0 + 28 * DAY) },
    n_samples: 40212,
    excluded: { bad: 12, findings: 96, windows: [[String(T0 + DAY), String(T0 + DAY + 3600e9)]] },
    thresholds: [
      { check_id: "tby.rate_of_change", param: "max_rate", value: 0.42, basis: "3 × the 99.9th percentile rate (0.14 per second)" },
      { check_id: "tby.spikes", param: "min_scale", value: 0.05, basis: "the larger of the resolution (0.01) and the noise floor (0.05)" },
      { check_id: "tby.flatline", param: "min_duration", value: null, basis: "no repeated values to learn from" },
      { check_id: "tby.drift", param: "max_slope", value: 1.76, basis: "4 × the median absolute slope" },
    ],
    band: { lo: 3.1, hi: 96.4, source: "profile" },
    profile: {},
    ...overrides,
  };
}

const PREVIOUS = baseline({
  version: 6,
  created_at: "2026-11-16T02:37:04Z",
  thresholds: [
    { check_id: "tby.rate_of_change", param: "max_rate", value: 0.31, basis: "3 × the 99.9th percentile rate (0.10 per second)" },
    { check_id: "tby.spikes", param: "min_scale", value: 0.05, basis: "the larger of the resolution (0.01) and the noise floor (0.05)" },
    { check_id: "tby.flatline", param: "min_duration", value: 600, basis: "the longest ordinary run of equal values" },
    { check_id: "tby.drift", param: "max_slope", value: 2, basis: "4 × the median absolute slope" },
  ],
});

type Backend = { items: Baseline[]; gets: string[]; posts: number; post: () => Response };

/** A fake backend whose list of baselines the test changes while the panel polls. */
function backend(items: Baseline[], options: Parameters<typeof stubFetch>[1] = {}): Backend {
  const state: Backend = { items, gets: [], posts: 0, post: () => json({ queued: true }, 202) };
  stubFetch((url, init) => {
    const [path, query = ""] = url.split("?");
    if (path !== URL) throw new Error(`unexpected ${url}`);
    if (init?.method === "POST") {
      state.posts += 1;
      return state.post();
    }
    state.gets.push(query);
    return { items: state.items, next_cursor: null };
  }, options);
  return state;
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BaselinePanel seriesId={SID} />
    </QueryClientProvider>,
  );
  return screen.getByRole("region", { name: "Baseline" });
}

/** A check's entry in the thresholds list: check, parameter, value, previous, change, basis;
 * a part the entry does not show is a dash. */
function rowOf(panel: HTMLElement, checkId: string): string[] {
  const item = within(panel).getByText(checkId).closest("li")!;
  return ["check", "param", "value", "previous", "change", "basis"].map(
    (part) => item.querySelector(`[data-part="${part}"]`)?.textContent ?? "—",
  );
}

const DEFAULTS = { ...baselinePolling };
beforeEach(() => {
  baselinePolling.intervalMs = 20;
  baselinePolling.timeoutMs = 5000;
});
afterEach(() => Object.assign(baselinePolling, DEFAULTS));

describe("series.baseline", () => {
  it("shows the baseline and the change against the previous version", async () => {
    const asked = backend([baseline(), PREVIOUS]);
    const panel = renderPanel();
    expect(await within(panel).findByText("Version 7")).toBeTruthy();
    expect(asked.gets[0]).toBe("limit=2");
    expect(panel.textContent).toContain(`computed ${formatLocalTime("2026-11-17T02:37:04Z")} (scheduled refresh)`);
    expect(panel.textContent).toContain(`${formatLocalTime(T0)} to ${formatLocalTime(T0 + 28 * DAY)}`);
    expect(panel.textContent).toContain(
      "40,212 samples used; 12 bad-quality samples and 96 samples inside 1 open critical finding window left out.",
    );

    const list = within(panel).getByRole("list", { name: "Learned thresholds of version 7, against version 6" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(baseline().thresholds.length);
    // The previous value and the change are said in words, next to the value.
    expect(within(panel).getByText("tby.rate_of_change").closest("li")?.textContent).toContain("0.42was 0.31 (+35%)");
    expect(rowOf(panel, "tby.rate_of_change")).toEqual([
      "tby.rate_of_change",
      "max_rate",
      "0.42",
      "0.31",
      "+35%",
      "3 × the 99.9th percentile rate (0.14 per second)",
    ]);
    expect(rowOf(panel, "tby.spikes").slice(2, 5)).toEqual(["0.05", "0.05", "no change"]);
    expect(rowOf(panel, "tby.flatline").slice(2)).toEqual(["—", "600", "—", "no repeated values to learn from"]);
    expect(rowOf(panel, "tby.drift").slice(2, 5)).toEqual(["1.76", "2", "−12%"]);
  });

  it("shows dashes without a previous version, and a manual refresh", async () => {
    backend([baseline({ version: 1, trigger: "manual", excluded: { bad: 0, findings: 0, windows: [] } })]);
    const panel = renderPanel();
    expect(await within(panel).findByText("Version 1")).toBeTruthy();
    expect(panel.textContent).toContain("(refreshed by hand)");
    expect(panel.textContent).toContain("40,212 samples used.");
    expect(panel.textContent).not.toContain("left out");
    expect(rowOf(panel, "tby.rate_of_change").slice(2, 5)).toEqual(["0.42", "—", "—"]);
  });

  it("says when there is no baseline and offers editors to compute one", async () => {
    backend([], asRole("editor"));
    const panel = renderPanel();
    expect(await within(panel).findByText("No baseline yet. Runs judge this series against the window they check.")).toBeTruthy();
    expect(within(panel).queryByRole("list")).toBeNull();
    expect(await within(panel).findByRole("button", { name: "Compute baseline" })).toBeTruthy();
  });

  it("gives viewers no button", async () => {
    const asked = backend([baseline(), PREVIOUS], asRole("viewer"));
    const panel = renderPanel();
    expect(await within(panel).findByText("Version 7")).toBeTruthy();
    // The role has arrived when the editor's button would have: wait for both requests.
    await waitFor(() => expect(asked.gets.length).toBeGreaterThan(0));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(within(panel).queryByRole("button")).toBeNull();
  });

  it("refreshes for editors and shows the new version when it arrives", async () => {
    const asked = backend([baseline(), PREVIOUS], asRole("editor"));
    const user = userEvent.setup();
    const panel = renderPanel();
    const button = await within(panel).findByRole("button", { name: "Refresh baseline" });
    await user.click(button);
    expect(asked.posts).toBe(1);
    const status = within(panel).getByRole("status");
    await waitFor(() => expect(status.textContent).toBe("Baseline is being computed…"));
    expect(status.getAttribute("aria-live")).toBe("polite");
    expect((button as HTMLButtonElement).disabled).toBe(true);

    // The poll goes on while the newest version is the one from before the click.
    const before = asked.gets.length;
    await waitFor(() => expect(asked.gets.length).toBeGreaterThan(before + 1));
    expect(status.textContent).toBe("Baseline is being computed…");

    asked.items = [
      baseline({
        version: 8,
        trigger: "manual",
        thresholds: [{ check_id: "tby.rate_of_change", param: "max_rate", value: 0.63, basis: "3 × the 99.9th percentile rate (0.21 per second)" }],
      }),
      baseline(),
    ];
    await waitFor(() => expect(status.textContent).toBe("Baseline updated to version 8."));
    expect(within(panel).getByText("Version 8")).toBeTruthy();
    expect(rowOf(panel, "tby.rate_of_change").slice(2, 5)).toEqual(["0.63", "0.42", "+50%"]);
    expect((button as HTMLButtonElement).disabled).toBe(false);

    // The poll has stopped.
    const after = asked.gets.length;
    await new Promise((resolve) => setTimeout(resolve, 80));
    expect(asked.gets.length).toBe(after);
  });

  it("computes the first baseline", async () => {
    const asked = backend([], asRole("editor"));
    const user = userEvent.setup();
    const panel = renderPanel();
    await user.click(await within(panel).findByRole("button", { name: "Compute baseline" }));
    await waitFor(() => expect(within(panel).getByRole("status").textContent).toBe("Baseline is being computed…"));
    asked.items = [baseline({ version: 1 })];
    await waitFor(() => expect(within(panel).getByRole("status").textContent).toBe("Baseline updated to version 1."));
    expect(within(panel).getByRole("button", { name: "Refresh baseline" })).toBeTruthy();
  });

  it("says when no new version came", async () => {
    baselinePolling.timeoutMs = 80;
    const asked = backend([baseline()], asRole("editor"));
    const user = userEvent.setup();
    const panel = renderPanel();
    await user.click(await within(panel).findByRole("button", { name: "Refresh baseline" }));
    await waitFor(() =>
      expect(within(panel).getByRole("status").textContent).toBe(
        "No new version: nothing changed since version 7, or there is too little usable data.",
      ),
    );
    const after = asked.gets.length;
    await new Promise((resolve) => setTimeout(resolve, 80));
    expect(asked.gets.length).toBe(after);
  });

  it("says when the cache holds no data, and reports other errors", async () => {
    const asked = backend([], asRole("editor"));
    asked.post = () => json({ detail: "no_data" }, 409);
    const user = userEvent.setup();
    const panel = renderPanel();
    const button = await within(panel).findByRole("button", { name: "Compute baseline" });
    await user.click(button);
    expect((await within(panel).findByRole("alert")).textContent).toBe("The cache holds no data of this series yet.");
    expect(within(panel).getByRole("status").textContent).toBe("");

    asked.post = () => json({ detail: "cache unavailable" }, 503);
    await user.click(button);
    await waitFor(() => expect(within(panel).getByRole("alert").textContent).toMatch(/^Could not refresh the baseline: /));
    expect(within(panel).getByRole("alert").textContent).toContain("cache unavailable");
  });

  it("reports a failed load", async () => {
    stubFetch(() => json({ detail: "boom" }, 500));
    const panel = renderPanel();
    expect((await within(panel).findByRole("alert")).textContent).toMatch(/^Could not load the baseline: /);
  });
});

describe("changeText", () => {
  it("signs and rounds the change", () => {
    expect(changeText(0.42, 0.31)).toBe("+35%");
    expect(changeText(1.76, 2)).toBe("−12%");
    expect(changeText(5, 5)).toBe("no change");
    expect(changeText(1.02, 1)).toBe("+2%");
    expect(changeText(100.01, 100)).toBe("+<0.1%");
    expect(changeText(-3, -2)).toBe("−50%");
    expect(changeText(null, 2)).toBe("—");
    expect(changeText(2, null)).toBe("—");
    expect(changeText(2, undefined)).toBe("—");
    expect(changeText(2, 0)).toBe("—");
  });
});

describe("excludedText", () => {
  it("counts in singular and plural, and is silent when nothing was left out", () => {
    expect(excludedText({ bad: 0, findings: 0, windows: [] })).toBeNull();
    expect(excludedText({ bad: 1, findings: 0, windows: [] })).toBe(
      "1 bad-quality sample and 0 samples inside 0 open critical finding windows left out",
    );
    expect(excludedText({ bad: 0, findings: 1500, windows: [["1", "2"], ["3", "4"]] })).toBe(
      "0 bad-quality samples and 1,500 samples inside 2 open critical finding windows left out",
    );
  });
});
