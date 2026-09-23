import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { Finding } from "../types";
import { json, makeRun, renderApp, stubFetch } from "./helpers";

/** A stored completeness finding one hour long. */
function finding(id: string, severity: Finding["severity"], start: number, summary: string): Finding {
  return {
    id,
    check_id: "tby.completeness",
    series_id: "22222222-2222-2222-2222-222222222222",
    dimension: "completeness",
    severity,
    window: { start, end: start + 3_600_000_000_000 },
    score_impact: 0.05,
    summary,
    evidence: { gap_start: start, gap_duration_ns: 3_600_000_000_000 },
    status: "open",
    status_reason: null,
    first_run_id: "r",
    last_run_id: "r",
    occurrences: id === "b" ? 2 : 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

describe("RunReport", () => {
  it("shows scores, findings sorted by severity with expandable evidence, skipped checks and the series", async () => {
    const run = makeRun({ stats: { n_samples: 72, n_findings: 2, skipped_checks: [{ check_id: "tby.latency", missing: "ingest_ts" }] } });
    const t = run.window!.start;
    stubFetch((url) => {
      if (url === `/api/runs/${run.id}`) return run;
      if (url.startsWith("/api/findings?run_id=")) return { items: [finding("a", "medium", t, "Short gap"), finding("b", "high", t + 7_200_000_000_000, "Long gap")], next_cursor: null };
      if (url.startsWith(`/api/series/${run.series[0]!.id}/scores`)) return { items: [{ series_id: "s", run_id: run.id, layer: "raw", method_version: "2", overall: 97.5, dimensions: { completeness: 91.2, validity: 100 }, n_findings: 2, computed_at: "2026-01-01T00:00:01Z" }], next_cursor: null };
      if (url === `/api/series/${run.series[0]!.id}`) return { id: "s", source_id: "u", external_id: "pump-1", name: "Pump 1", unit: "bar", kind: "measurement", physical_min: 0, physical_max: 16, operational_min: null, operational_max: null, latest_score: null, open_findings: 2, n_runs: 1, last_run_at: null };
      throw new Error(`unexpected ${url}`);
    });
    const user = userEvent.setup();
    renderApp(`/runs/${run.id}`);

    expect(await screen.findByText("91.2")).toBeTruthy();
    const list = (await screen.findByRole("heading", { name: "Findings (2)" })).parentElement!;
    const rows = within(list).getAllByRole("button");
    expect(rows.map((b) => b.textContent?.match(/(Long|Short) gap/)?.[0])).toEqual(["Long gap", "Short gap"]);
    expect(within(list).getByText("seen in 2 runs")).toBeTruthy();

    expect(rows[0]!.getAttribute("aria-expanded")).toBe("false");
    await user.click(rows[0]!);
    expect(rows[0]!.getAttribute("aria-expanded")).toBe("true");
    const panel = document.getElementById(rows[0]!.getAttribute("aria-controls")!)!;
    expect(panel.hidden).toBe(false);
    expect(within(panel).getByText("1h")).toBeTruthy();

    expect(screen.getByText("tby.latency")).toBeTruthy();
    expect(await screen.findByText("0 to 16")).toBeTruthy();
    expect(screen.getByText("Run succeeded").getAttribute("aria-live") ?? screen.getByText("Run succeeded").closest("[aria-live]")?.getAttribute("aria-live")).toBe("polite");
  });

  it("keeps the stored overall score when the score row fails and retries on request", async () => {
    const run = makeRun();
    let scoreCalls = 0;
    stubFetch((url) => {
      if (url === `/api/runs/${run.id}`) return run;
      if (url.startsWith("/api/findings?run_id=")) return { items: [], next_cursor: null };
      if (url.startsWith(`/api/series/${run.series[0]!.id}/scores`)) {
        scoreCalls += 1;
        if (scoreCalls === 1) return json({ detail: "database unavailable" }, 503);
        return { items: [{ series_id: "s", run_id: run.id, layer: "raw", method_version: "2", overall: 97.5, dimensions: { completeness: 91.2 }, n_findings: 0, computed_at: "2026-01-01T00:00:01Z" }], next_cursor: null };
      }
      if (url === `/api/series/${run.series[0]!.id}`) return { id: "s", source_id: "u", external_id: "pump-1", name: "Pump 1", unit: null, kind: "measurement", physical_min: null, physical_max: null, operational_min: null, operational_max: null, latest_score: null, open_findings: 0, n_runs: 1, last_run_at: null };
      throw new Error(`unexpected ${url}`);
    });
    const user = userEvent.setup();
    renderApp(`/runs/${run.id}`);

    const alert = await screen.findByText(/Could not load the dimension scores: database unavailable/);
    const tiles = screen.getByRole("region", { name: "Scores" });
    expect(within(tiles).getByText(run.series[0]!.score.toFixed(1))).toBeTruthy();
    await user.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("91.2")).toBeTruthy();
    expect(screen.queryByText(/Could not load the dimension scores/)).toBeNull();
  });

  it("shows the error and a way back for a failed run", async () => {
    const run = makeRun({ status: "failed", error: "cannot parse CSV: column 'ts' missing", stats: {} });
    stubFetch((url) => {
      if (url === `/api/runs/${run.id}`) return run;
      throw new Error(`unexpected ${url}`);
    });
    renderApp(`/runs/${run.id}`);
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("cannot parse CSV: column 'ts' missing")).toBeTruthy();
    expect(within(alert).getByRole("link", { name: "Try again" }).getAttribute("href")).toBe("/runs/new");
  });
});
