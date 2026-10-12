import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { dialogs } from "../pages/admin/ui";
import { json, renderApp, stubFetch } from "./helpers";
import { DROP_ID, OPC_ID, PI_ID, SHA, asRole, detail, dropDetail, file, history, job } from "./sourcesFake";

afterEach(() => vi.restoreAllMocks());

type Sent = { url: string; method: string; body: unknown };

/** A fake backend for one source; jobs answer as `jobs[id]` (a list is served in order). */
function backend(source = detail(), jobs: Record<string, ReturnType<typeof job>[]> = {}, extra: (url: string, init?: RequestInit) => unknown = () => undefined) {
  const sent: Sent[] = [];
  let current = source;
  const base = `/api/sources/${source.id}`;
  const route = (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (method !== "GET") sent.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : null });
    const more = extra(url, init);
    if (more !== undefined) return more;
    if (url === base && method === "GET") return current;
    if (url === base && method === "PATCH") {
      const body = JSON.parse(String(init?.body)) as Partial<typeof current>;
      current = { ...current, ...body, config: (body.config as Record<string, unknown>) ?? current.config };
      return current;
    }
    if (url === `${base}/fetches?limit=20`) return history();
    if (url === `${base}/check` || url === `${base}/search` || url === `${base}/metadata` || url === `${base}/fetches` || url === `${base}/ingest`)
      return json({ id: url.split("/").pop() === "fetches" ? "fetch" : url.split("/").pop() }, 202);
    const jobMatch = /\/fetches\/([^/?]+)$/.exec(url);
    if (jobMatch) {
      const queue = jobs[jobMatch[1] as string] ?? [];
      return queue.length > 1 ? queue.shift() : queue[0];
    }
    if (url === `${base}/credentials`) return new Response(null, { status: 204 });
    if (url === `${base}/series`) return { created: 1, existing: 1 };
    throw new Error(`unexpected ${method} ${url}`);
  };
  return { route, sent };
}

describe("SourceDetail of a file drop", () => {
  const files = (url: string) => {
    if (url === `/api/sources/${DROP_ID}/files?limit=50`) {
      return { items: [file(), file({ id: "f-2", path: "bad.csv", status: "failed", rows: 0, n_series: 0, n_new_series: 0, start_at: null, end_at: null, error: "column 'value' is missing" })], next_cursor: "next" };
    }
    if (url === `/api/sources/${DROP_ID}/files?limit=50&cursor=next`) return { items: [file({ id: "f-3", path: "old.csv", versions: 2 })], next_cursor: null };
    if (url === `/api/sources/${DROP_ID}/files?limit=50&status=failed`) return { items: [], next_cursor: null };
    return undefined;
  };

  it("offers Check and Scan now instead of Fetch, Search and Metadata", async () => {
    const scanned = job({
      trigger: "ingest",
      calls: 2,
      rows: 1440,
      status: "partial",
      error: "1 file(s) failed: bad.csv: column 'value' is missing",
      result: {
        files: [
          { path: "2026/jan.csv", status: "ingested", rows: 1440, series: 2, new_series: 2, error: null },
          { path: "bad.csv", status: "failed", rows: 0, series: 0, new_series: 0, error: "column 'value' is missing" },
        ],
        known: 3,
        settling: 1,
        too_large: [],
        outside_root: [],
        more: true,
      },
    });
    const checked = job({ result: { files: 4, bytes: 1_500_000 } });
    const { route, sent } = backend(dropDetail(), { ingest: [scanned], check: [checked] }, files);
    stubFetch(route, asRole("editor"));
    const user = userEvent.setup();
    renderApp(`/sources/${DROP_ID}`);
    await user.click(await screen.findByRole("button", { name: "Check folder" }));
    expect(await screen.findByText("The folder holds 4 matching files (1.5 MB).")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Fetch" })).toBeNull();
    expect(screen.queryByLabelText("Find points")).toBeNull();
    expect(screen.queryByRole("button", { name: "Import metadata" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Scan now" }));
    expect(
      await screen.findByText("Read 2 files (1 failed), 3 already read, 1 still being copied. More files wait for the next scan."),
    ).toBeTruthy();
    expect(sent.map((s) => `${s.method} ${s.url}`)).toEqual([`POST /api/sources/${DROP_ID}/check`, `POST /api/sources/${DROP_ID}/ingest`]);
  });

  it("lists files with their errors, filters them and loads more", async () => {
    const { route } = backend(dropDetail(), {}, files);
    stubFetch(route, asRole("viewer"));
    const user = userEvent.setup();
    renderApp(`/sources/${DROP_ID}`);
    const table = await screen.findByRole("table", { name: "The source's files, last read first" });
    expect(within(table).getByText("2026/jan.csv")).toBeTruthy();
    expect(within(table).getByText("column 'value' is missing")).toBeTruthy();
    expect(within(table).getAllByText("2.0 kB")).toHaveLength(2);
    await user.click(screen.getByRole("button", { name: "Load more" }));
    expect(await within(table).findByText("old.csv")).toBeTruthy();
    expect(within(table).getByText("2.0 kB · version 2")).toBeTruthy();
    await user.selectOptions(screen.getByLabelText("File status"), "failed");
    expect(await screen.findByText("No files read yet. Drop files into the folder, then scan.")).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Actions" })).toBeNull();
  });
});

describe("SourceDetail", () => {
  it("shows viewers health and history but no actions or settings", async () => {
    const { route } = backend();
    stubFetch(route, asRole("viewer"));
    renderApp(`/sources/${PI_ID}`);
    expect(await screen.findByRole("heading", { name: "Health" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "History" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Actions" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Settings" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Credentials" })).toBeNull();
  });

  it("gives editors actions only, and follows a check to its end", async () => {
    const { route, sent } = backend(detail(), { check: [job({ status: "queued", finished_at: null }), job({ status: "succeeded" })] });
    stubFetch(route, asRole("editor"));
    const user = userEvent.setup();
    renderApp(`/sources/${PI_ID}`);
    await user.click(await screen.findByRole("button", { name: "Check connection" }));
    expect(await screen.findByText("Waiting for the worker…")).toBeTruthy();
    expect(await screen.findByText("succeeded", {}, { timeout: 4000 })).toBeTruthy();
    expect(sent.map((s) => `${s.method} ${s.url}`)).toEqual([`POST /api/sources/${PI_ID}/check`]);
    expect(screen.queryByRole("heading", { name: "Settings" })).toBeNull();
  });

  it("searches, then adds the picked points as series", async () => {
    const items = [
      { external_id: "\\\\PISRV01\\FIC101.PV", name: "FIC101.PV", unit: "m3/h", description: "Inlet flow" },
      { external_id: "\\\\PISRV01\\FIC102.PV", name: "FIC102.PV", unit: "m3/h", description: null },
    ];
    const { route, sent } = backend(detail(), { search: [job({ trigger: "search", result: { items, truncated: false } })] });
    stubFetch(route, asRole("editor"));
    const user = userEvent.setup();
    renderApp(`/sources/${PI_ID}`);
    await user.type(await screen.findByLabelText("Find points"), "FIC1");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await user.click(await screen.findByRole("checkbox", { name: /FIC102\.PV/ }));
    await user.click(screen.getByRole("button", { name: "Add 1 as series" }));
    expect(await screen.findByText("Added 1 new series, 1 already existed.")).toBeTruthy();
    expect(sent.find((s) => s.url.endsWith("/search"))?.body).toEqual({ query: "FIC1", limit: 100 });
    expect(sent.find((s) => s.url.endsWith("/series"))?.body).toEqual([{ external_id: "\\\\PISRV01\\FIC102.PV", name: "FIC102.PV", unit: "m3/h" }]);
  });

  it("refuses a fetch window that ends before it starts", async () => {
    const { route, sent } = backend();
    stubFetch(route, asRole("editor"));
    const user = userEvent.setup();
    renderApp(`/sources/${PI_ID}`);
    const form = await screen.findByRole("form", { name: "Fetch a window" });
    fireEvent.change(within(form).getByLabelText("From"), { target: { value: "2026-10-09T12:00" } });
    fireEvent.change(within(form).getByLabelText("To"), { target: { value: "2026-10-09T06:00" } });
    await user.click(within(form).getByRole("button", { name: "Fetch" }));
    expect(within(form).getByRole("alert").textContent).toBe("To must be later than From.");
    expect(sent).toEqual([]);
  });

  it("shows a metadata import's result", async () => {
    const result = { updated: 1, unchanged: 0, failed: 1, series: { s1: { changed: ["unit", "metadata"] }, s2: { error: "not found in PI" } } };
    const { route, sent } = backend(detail(), { metadata: [job({ trigger: "metadata", status: "partial", error: "1 series failed: not found in PI", result })] });
    stubFetch(route, asRole("editor"));
    const user = userEvent.setup();
    renderApp(`/sources/${PI_ID}`);
    await user.click(await screen.findByRole("checkbox", { name: "Also replace values edited by hand" }));
    await user.click(screen.getByRole("button", { name: "Import metadata" }));
    expect(await screen.findByText("1 updated, 0 unchanged, 1 failed.")).toBeTruthy();
    expect(screen.getByText("unit, metadata")).toBeTruthy();
    expect(sent[0]?.body).toEqual({ overwrite: true });
  });

  it("lets admins set and remove PI credentials, never showing them", async () => {
    const { route, sent } = backend();
    stubFetch(route);
    vi.spyOn(dialogs, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    renderApp(`/sources/${PI_ID}`);
    const form = await screen.findByRole("form", { name: "Credentials" });
    await user.type(within(form).getByLabelText("Username"), "PLANT\\svc");
    await user.type(within(form).getByLabelText("Password"), "s3cret");
    await user.click(within(form).getByRole("button", { name: "Save credentials" }));
    expect(await screen.findByText("Credentials saved.")).toBeTruthy();
    expect((within(form).getByLabelText("Password") as HTMLInputElement).value).toBe("");
    await user.selectOptions(within(form).getByLabelText("Kind"), "bearer");
    await user.type(within(form).getByLabelText("Token"), "abc.def");
    await user.click(within(form).getByRole("button", { name: "Save credentials" }));
    await user.click(screen.getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(sent.filter((s) => s.url.endsWith("/credentials"))).toHaveLength(3));
    expect(sent.filter((s) => s.url.endsWith("/credentials")).map((s) => [s.method, s.body])).toEqual([
      ["PUT", { kind: "basic", username: "PLANT\\svc", password: "s3cret" }],
      ["PUT", { kind: "bearer", token: "abc.def" }],
      ["DELETE", null],
    ]);
  });

  it("keeps hidden config keys when admins edit settings, and toggles enabled", async () => {
    const { route, sent } = backend();
    stubFetch(route);
    const user = userEvent.setup();
    renderApp(`/sources/${PI_ID}`);
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const form = screen.getByRole("form", { name: "Source settings" });
    await user.type(within(form).getByLabelText("AF database (optional)"), "\\\\AFSRV01\\Plant");
    await user.click(within(form).getByRole("button", { name: "Save" }));
    await user.click(await screen.findByRole("button", { name: "Disable" }));
    await screen.findByText("(disabled)");
    expect(sent.map((s) => s.body)).toEqual([
      {
        name: "PI plant",
        config: {
          base_url: "https://pi.example.com/piwebapi",
          data_server: "\\\\PISRV01",
          settle_s: 600,
          asset_database: "\\\\AFSRV01\\Plant",
        },
      },
      { enabled: false },
    ]);
  });

  it("pins an OPC UA server certificate and shows the client certificate", async () => {
    const opc = detail({
      id: OPC_ID,
      type: "opc_ua",
      name: "SCADA",
      config: { endpoint_url: "opc.tcp://scada:4840" },
      health: {
        status: "degraded",
        last_error: { at: "2026-10-10T08:00:00Z", message: `server certificate not pinned: SHA-256 ${SHA}, subject SCADA server; set …`, retryable: false },
      },
    });
    const certificate = { certificate_pem: "-----BEGIN CERTIFICATE-----", sha1: "11".repeat(20), sha256: "22".repeat(32), application_uri: `urn:tabayyun:source:${OPC_ID}`, not_after: "2031-10-10T00:00:00Z" };
    const { route, sent } = backend(opc, {}, (url) => (url.endsWith("/client-certificate") ? certificate : undefined));
    stubFetch(route);
    const confirm = vi.spyOn(dialogs, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    renderApp(`/sources/${OPC_ID}`);
    expect(await screen.findByText("SCADA server")).toBeTruthy();
    expect(await screen.findByText("22".repeat(32))).toBeTruthy();
    expect(screen.getByText(`urn:tabayyun:source:${OPC_ID}`)).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Pin this certificate" }));
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(confirm).toHaveBeenCalledWith(`Pin the server certificate ${SHA}?`);
    expect(sent[0]?.body).toEqual({ config: { endpoint_url: "opc.tcp://scada:4840", server_certificate_sha256: SHA } });
    await waitFor(() => expect(screen.getByTestId("server-certificate").textContent).toBe(`Server certificate: ${SHA}`));
  });
});
