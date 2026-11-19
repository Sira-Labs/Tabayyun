import { screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { live } from "../hooks/useLiveEvents";
import { backend, fakeLayout, finding, listRequests } from "./findingsFake";
import { makeRun, renderApp, stubFetch, WORKSPACE } from "./helpers";
import { fakeEventSource, FakeEventSource } from "./liveFake";

const TIMING = { ...live };
const settle = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

beforeEach(() => {
  live.gatherMs = 20;
  live.reopenMs = 40;
  fakeLayout();
});
afterEach(() => {
  Object.assign(live, TIMING);
});

/** The one stream the app holds, once it has opened it. */
async function stream(opened: FakeEventSource[], n = 1): Promise<FakeEventSource> {
  await waitFor(() => expect(opened).toHaveLength(n));
  return opened[n - 1] as FakeEventSource;
}

describe("live.events", () => {
  it("opens the stream of the current workspace and closes it with the page", async () => {
    const opened = fakeEventSource();
    backend([finding(1)]);
    const app = renderApp("/findings");
    const source = await stream(opened);
    expect(source.url).toBe(`/api/events?workspace=${WORKSPACE.id}`);
    await screen.findByText("Spike number 1");
    app.unmount();
    expect(source.readyState).toBe(FakeEventSource.CLOSED);
    expect(opened).toHaveLength(1);
  });

  it("shows a new finding in the inbox after finding.created, without a reload by the user", async () => {
    const opened = fakeEventSource();
    const asked = backend([finding(1)]);
    renderApp("/findings");
    const source = await stream(opened);
    source.accept();
    await screen.findByText("Spike number 1");
    asked.items = [finding(2), finding(1)];
    source.emit("finding.created", { run_id: "r1", count: 1 });
    expect(await screen.findByText("Spike number 2")).toBeTruthy();
  });

  it("gathers the events of a moment into one reload", async () => {
    const opened = fakeEventSource();
    const asked = backend([finding(1)]);
    renderApp("/findings");
    const source = await stream(opened);
    source.accept();
    await screen.findByText("Spike number 1");
    const before = listRequests(asked).length;
    for (let n = 0; n < 5; n += 1) {
      source.emit("run.completed", { run_id: `r${n}`, status: "succeeded", n_findings_new: 1 });
      source.emit("finding.created", { run_id: `r${n}`, count: 1 });
    }
    await waitFor(() => expect(listRequests(asked).length).toBe(before + 1));
    await settle(80);
    expect(listRequests(asked).length).toBe(before + 1);
  });

  it("does not reload the inbox for a run that stored no new findings", async () => {
    const opened = fakeEventSource();
    const asked = backend([finding(1)]);
    renderApp("/findings");
    const source = await stream(opened);
    source.accept();
    await screen.findByText("Spike number 1");
    const before = listRequests(asked).length;
    source.emit("run.completed", { run_id: "r1", status: "failed", n_findings_new: 0 });
    await settle(80);
    expect(listRequests(asked).length).toBe(before);
  });

  it("shows a finished run in the runs list after run.completed", async () => {
    const opened = fakeEventSource();
    let runs = [makeRun({ id: "00000000-0000-0000-0000-000000000001", status: "running" })];
    stubFetch((url) => {
      if (url === "/api/runs?limit=25") return { items: runs, next_cursor: null };
      throw new Error(`unexpected ${url}`);
    });
    renderApp("/runs");
    const source = await stream(opened);
    source.accept();
    expect(await screen.findByText("running")).toBeTruthy();
    runs = [makeRun({ id: "00000000-0000-0000-0000-000000000001", status: "succeeded" })];
    source.emit("run.completed", { run_id: runs[0]?.id, status: "succeeded", n_findings_new: 0 });
    expect(await screen.findByText("succeeded")).toBeTruthy();
    expect(screen.queryByText("running")).toBeNull();
  });

  it("reloads after resync, and after the stream was lost and is back", async () => {
    const opened = fakeEventSource();
    const asked = backend([finding(1)]);
    renderApp("/findings");
    const source = await stream(opened);
    source.accept();
    await screen.findByText("Spike number 1");
    // The first open reloads nothing: the page has just loaded.
    await settle(60);
    const first = listRequests(asked).length;

    source.emit("resync");
    await waitFor(() => expect(listRequests(asked).length).toBe(first + 1));

    source.drop();
    await settle(60);
    expect(listRequests(asked).length).toBe(first + 1);
    source.accept();
    await waitFor(() => expect(listRequests(asked).length).toBe(first + 2));
    expect(opened).toHaveLength(1);
  });

  it("opens a new stream some time after the server refused one", async () => {
    const opened = fakeEventSource();
    const asked = backend([finding(1)]);
    renderApp("/findings");
    const refused = await stream(opened);
    await screen.findByText("Spike number 1");
    const before = listRequests(asked).length;
    refused.refuse();
    refused.refuse();
    const second = await stream(opened, 2);
    expect(second.url).toBe(refused.url);
    // It was away: once it is open, the page reloads.
    second.accept();
    await waitFor(() => expect(listRequests(asked).length).toBe(before + 1));
    await settle(100);
    expect(opened).toHaveLength(2);
  });

  it("does nothing where the browser has no EventSource", async () => {
    backend([finding(1)]);
    renderApp("/findings");
    expect(await screen.findByText("Spike number 1")).toBeTruthy();
  });
});
