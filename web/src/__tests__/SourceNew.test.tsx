import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { json, renderApp, stubFetch } from "./helpers";
import { OPC_ID, PI_ID, asRole, detail, history } from "./sourcesFake";

describe("SourceNew", () => {
  it("creates a PI Web API source and opens it", async () => {
    let body: unknown;
    stubFetch((url, init) => {
      if (url === "/api/sources" && init?.method === "POST") {
        body = JSON.parse(String(init.body));
        return json(detail(), 201);
      }
      if (url === `/api/sources/${PI_ID}`) return detail();
      if (url.startsWith(`/api/sources/${PI_ID}/fetches`)) return history();
      throw new Error(`unexpected ${url}`);
    });
    const user = userEvent.setup();
    const { router } = renderApp("/sources/new");
    await user.type(await screen.findByLabelText("Name"), "PI plant");
    await user.type(screen.getByLabelText("Base URL"), "https://pi.example.com/piwebapi");
    await user.type(screen.getByLabelText("Data server"), "\\\\PISRV01");
    await user.type(screen.getByLabelText("Poll interval (seconds, empty for none)"), "300");
    await user.click(screen.getByRole("button", { name: "Create source" }));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/sources/${PI_ID}`));
    expect(body).toEqual({
      type: "pi_web_api",
      name: "PI plant",
      config: { base_url: "https://pi.example.com/piwebapi", data_server: "\\\\PISRV01", poll_interval_s: 300 },
    });
  });

  it("shows the server's field errors for an OPC UA source", async () => {
    let body: { config?: Record<string, unknown> } = {};
    stubFetch((url, init) => {
      if (url === "/api/sources" && init?.method === "POST") {
        body = JSON.parse(String(init.body));
        return json(
          { detail: "invalid_config", errors: [{ loc: ["endpoint_url"], msg: "Value error, must be an opc.tcp URL", type: "value_error" }] },
          422,
        );
      }
      throw new Error(`unexpected ${url}`);
    });
    const user = userEvent.setup();
    renderApp("/sources/new");
    await user.selectOptions(await screen.findByLabelText("Type"), "opc_ua");
    await user.type(screen.getByLabelText("Name"), "SCADA");
    await user.type(screen.getByLabelText("Endpoint URL"), "http://scada");
    await user.click(screen.getByRole("button", { name: "Create source" }));
    expect(await screen.findByText("endpoint_url: Value error, must be an opc.tcp URL")).toBeTruthy();
    expect(body.config).toEqual({ endpoint_url: "http://scada", security_policy: "Basic256Sha256", security_mode: "SignAndEncrypt" });
    void OPC_ID;
  });

  it("refuses invalid synthetic JSON without a request", async () => {
    const fetch = stubFetch(() => {
      throw new Error("no request expected");
    });
    const user = userEvent.setup();
    renderApp("/sources/new");
    await user.selectOptions(await screen.findByLabelText("Type"), "synthetic");
    await user.type(screen.getByLabelText("Name"), "demo");
    const box = screen.getByLabelText("Config (JSON)");
    await user.clear(box);
    await user.type(box, "{{not json");
    await user.click(screen.getByRole("button", { name: "Create source" }));
    expect(await screen.findByText("The config is not valid JSON.")).toBeTruthy();
    expect(fetch.mock.calls.some(([u]) => String(u) === "/api/sources")).toBe(false);
  });

  it("tells editors only admins create sources", async () => {
    stubFetch(() => ({}), asRole("editor"));
    renderApp("/sources/new");
    expect(await screen.findByText("Only workspace admins create sources.")).toBeTruthy();
  });
});
