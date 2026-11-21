import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { live } from "../hooks/useLiveEvents";
import { renderApp } from "./helpers";
import { fakeEventSource, type FakeEventSource } from "./liveFake";
import { overview, overviewBackend, worstItem } from "./overviewFake";

const TIMING = { ...live };
const settle = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

beforeEach(() => {
  live.gatherMs = 20;
});
afterEach(() => {
  Object.assign(live, TIMING);
});

/** The stream the app opened, accepted, once the reload its opening causes is over. */
async function acceptedStream(opened: FakeEventSource[], asked: () => number): Promise<FakeEventSource> {
  await waitFor(() => expect(opened).toHaveLength(1));
  const source = opened[0] as FakeEventSource;
  const before = asked();
  source.accept();
  await waitFor(() => expect(asked()).toBeGreaterThan(before));
  await settle(40);
  return source;
}

describe("overview.live", () => {
  it("reloads the overview after run.completed", async () => {
    const opened = fakeEventSource();
    let worst = [worstItem()];
    const backend = overviewBackend(() => overview({ worst: { share: 0.05, n: worst.length, items: worst } }));
    renderApp("/");
    await screen.findByRole("link", { name: "Feeder 4 pressure" });
    const source = await acceptedStream(opened, () => backend.asked.length);

    worst = [worstItem({ name: "Feeder 9 level" })];
    source.emit("run.completed", { run_id: "r1", status: "succeeded", n_findings_new: 3 });
    const table = await screen.findByRole("table", { name: "Worst series, lowest value first" });
    expect(await within(table).findByRole("link", { name: "Feeder 9 level" })).toBeTruthy();
  });

  it("reloads the overview after finding.created", async () => {
    const opened = fakeEventSource();
    const backend = overviewBackend();
    renderApp("/");
    await screen.findByRole("region", { name: "Scores" });
    const source = await acceptedStream(opened, () => backend.asked.length);
    const before = backend.asked.length;
    source.emit("finding.created", { run_id: "r1", count: 1 });
    await waitFor(() => expect(backend.asked.length).toBe(before + 1));
  });
});
