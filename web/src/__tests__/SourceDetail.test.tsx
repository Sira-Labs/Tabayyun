import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { dialogs } from "../pages/admin/ui";
import { json, renderApp, stubFetch } from "./helpers";
import { OPC_ID, PI_ID, SHA, asRole, detail, history, job } from "./sourcesFake";

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
    if (url === `${base}/check` || url === `${base}/search` || url === `${base}/metadata` || url === `${base}/fetches`)
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
