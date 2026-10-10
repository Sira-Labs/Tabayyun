import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { renderApp, stubFetch } from "./helpers";
import { OPC_ID, PI_ID, asRole, summary } from "./sourcesFake";

const SOURCES = {
  items: [
    summary(),
    summary({ id: OPC_ID, type: "opc_ua", name: "SCADA", health_status: "failing", last_fetch: null, enabled: false }),
    summary({ id: "u", type: "upload", name: "Uploads", connector: false, health_status: "unknown", last_fetch: null }),
  ],
};

describe("SourcesList", () => {
  it("lists sources with health and last fetch, and offers New source to admins", async () => {
    stubFetch((url) => {
      if (url === "/api/sources") return SOURCES;
      throw new Error(`unexpected ${url}`);
    });
    renderApp("/sources");
    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((r) => within(r).getAllByRole("cell")[1]?.textContent)).toEqual(["PI Web API", "OPC UA", "Uploads"]);
    expect(within(rows[0] as HTMLElement).getByRole("link", { name: "PI plant" }).getAttribute("href")).toBe(`/sources/${PI_ID}`);
    expect(within(rows[0] as HTMLElement).getByText("succeeded")).toBeTruthy();
    expect(within(rows[1] as HTMLElement).getByText("failing")).toBeTruthy();
    expect(within(rows[1] as HTMLElement).getByText("(disabled)")).toBeTruthy();
    expect(within(rows[2] as HTMLElement).queryByRole("link")).toBeNull();
    expect(screen.getByRole("link", { name: "New source" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Sources" })).toBeTruthy();
  });

  it("hides New source from editors", async () => {
    stubFetch(() => SOURCES, asRole("editor"));
    renderApp("/sources");
    await screen.findByRole("table");
    expect(screen.queryByRole("link", { name: "New source" })).toBeNull();
  });
});
