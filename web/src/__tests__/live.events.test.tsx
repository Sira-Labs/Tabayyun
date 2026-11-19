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

/** Accept the stream and wait until the reload its opening causes is over. */
async function accepted(source: FakeEventSource, asked: { gets: string[] }) {
  const before = asked.gets.length;
  source.accept();
  await waitFor(() => expect(asked.gets.length).toBeGreaterThan(before));
  await settle(40);
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
    await screen.findByText("Spike number 1");
    await accepted(source, asked);
    asked.items = [finding(2), finding(1)];
    source.emit("finding.created", { run_id: "r1", count: 1 });
    expect(await screen.findByText("Spike number 2")).toBeTruthy();
  });

  it("reloads when the stream first opens: a finding stored before it listened is not missed", async () => {
    const opened = fakeEventSource();
    const asked = backend([finding(1)]);
    renderApp("/findings");
    const source = await stream(opened);
    await screen.findByText("Spike number 1");
    // Stored after the page asked for its list and before the stream was accepted: no event.
    asked.items = [finding(2), finding(1)];
    source.accept();
    expect(await screen.findByText("Spike number 2")).toBeTruthy();
  });

  it("reloads a list whose first request was still running when the stream opened", async () => {
    const opened = fakeEventSource();
    const asked = backend([finding(1)]);
    // The first list request is answered late, with what the server had when it was asked.
    let answer: (page: { items: ReturnType<typeof finding>[]; next_cursor: null }) => void = () => undefined;
    let lists = 0;
    asked.list = () => {
      lists += 1;
      if (lists > 1) return { items: asked.items, next_cursor: null };
      return new Promise((resolve) => (answer = resolve)) as unknown as ReturnType<typeof asked.list>;
    };
    renderApp("/findings");
    const source = await stream(opened);
    await waitFor(() => expect(lists).toBe(1));
    asked.items = [finding(2), finding(1)];
    source.accept();
    // The reload the open asks for comes while the first request is still out.
    await settle(60);
    expect(lists).toBe(1);
    answer({ items: [finding(1)], next_cursor: null });
    expect(await screen.findByText("Spike number 2")).toBeTruthy();
    expect(lists).toBe(2);
  });

  it("gathers the events of a moment into one reload", async () => {
    const opened = fakeEventSource();
    const asked = backend([finding(1)]);
    renderApp("/findings");
    const source = await stream(opened);
    await screen.findByText("Spike number 1");
    await accepted(source, asked);
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
    await screen.findByText("Spike number 1");
    await accepted(source, asked);
    const before = listRequests(asked).length;
    source.emit("run.completed", { run_id: "r1", status: "failed", n_findings_new: 0 });
    await settle(80);
    expect(listRequests(asked).length).toBe(before);
  });

  it("shows a finished run in the runs list after run.completed", async () => {
    const opened = fakeEventSource();
    const id = "00000000-0000-0000-0000-000000000001";
    let runs = [makeRun({ id, status: "running" })];
    let asks = 0;
    stubFetch((url) => {
      if (url !== "/api/runs?limit=25") throw new Error(`unexpected ${url}`);
      asks += 1;
      return { items: runs, next_cursor: null };
    });
    renderApp("/runs");
    const source = await stream(opened);
    expect(await screen.findByText("running")).toBeTruthy();
    source.accept();
    await waitFor(() => expect(asks).toBe(2));
    await settle(40);
    runs = [makeRun({ id, status: "succeeded" })];
    source.emit("run.completed", { run_id: id, status: "succeeded", n_findings_new: 0 });
    expect(await screen.findByText("succeeded")).toBeTruthy();
    expect(screen.queryByText("running")).toBeNull();
    // The run's record changed once more after it completed: the list is asked again.
    const before = asks;
    source.emit("run.updated", { run_id: id });
    await waitFor(() => expect(asks).toBe(before + 1));
  });

  it("reloads after resync, and after the stream was lost and is back", async () => {
    const opened = fakeEventSource();
    const asked = backend([finding(1)]);
    renderApp("/findings");
    const source = await stream(opened);
    await screen.findByText("Spike number 1");
    await accepted(source, asked);
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
