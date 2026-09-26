import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { browser, type Device } from "../auth";
import { ME, renderApp, stubFetch } from "./helpers";

afterEach(() => vi.restoreAllMocks());

const FIREFOX = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0";
const CHROME = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36";

function device(id: string, current: boolean, userAgent: string, method = "google"): Device {
  return {
    id,
    current,
    sign_in_method: method,
    created_at: "2026-09-26T10:00:00Z",
    last_seen_at: "2026-09-26T11:00:00Z",
    user_agent: userAgent,
    ip_address: "203.0.113.7",
  };
}

describe("Devices", () => {
  it("lists devices and signs out another one", async () => {
    let devices = [device("d1", true, CHROME), device("d2", false, FIREFOX, "passkey")];
    const fetch = stubFetch((url, init) => {
      if (url === "/api/auth/sessions") return devices;
      if (url === "/api/auth/sessions/d2" && init?.method === "DELETE") {
        devices = devices.filter((d) => d.id !== "d2");
        return new Response(null, { status: 204 });
      }
      if (url === "/api/auth/sessions/revoke-others" && init?.method === "POST") {
        devices = devices.filter((d) => d.current);
        return new Response(null, { status: 204 });
      }
      throw new Error(`unexpected ${init?.method ?? "GET"} ${url}`);
    });
    const user = userEvent.setup();
    renderApp("/settings/account");

    const list = await screen.findByRole("list");
    expect(within(list).getAllByRole("listitem").map((li) => li.getAttribute("aria-label"))).toEqual([
      "Chrome on Linux",
      "Firefox on Windows",
    ]);
    expect(within(screen.getByRole("listitem", { name: "Chrome on Linux" })).getByText("This device")).toBeTruthy();
    expect(screen.getByText(/Signed in as/).textContent).toContain(ME.user.email);
    expect(screen.getByRole("note").textContent).toContain("passkey");
    expect(screen.getByRole("link", { name: "Manage passkeys" }).getAttribute("href")).toBe("/api/auth/passkeys");

    await user.click(within(screen.getByRole("listitem", { name: "Firefox on Windows" })).getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(screen.queryByRole("listitem", { name: "Firefox on Windows" })).toBeNull());
    expect(screen.getByRole("button", { name: "Sign out all other devices" }).hasAttribute("disabled")).toBe(true);
    expect(fetch.mock.calls.some(([url, init]) => url === "/api/auth/sessions/d2" && init?.method === "DELETE")).toBe(true);
  });

  it("signs out all other devices", async () => {
    let devices = [device("d1", true, CHROME), device("d2", false, FIREFOX), device("d3", false, FIREFOX, "github")];
    stubFetch((url, init) => {
      if (url === "/api/auth/sessions") return devices;
      if (url === "/api/auth/sessions/revoke-others" && init?.method === "POST") {
        devices = devices.filter((d) => d.current);
        return new Response(null, { status: 204 });
      }
      throw new Error(`unexpected ${init?.method ?? "GET"} ${url}`);
    });
    renderApp("/settings/account");

    await screen.findByRole("listitem", { name: "Chrome on Linux" });
    expect(screen.getAllByRole("listitem")).toHaveLength(3);
    await userEvent.setup().click(screen.getByRole("button", { name: "Sign out all other devices" }));
    await waitFor(() => expect(screen.getAllByRole("listitem")).toHaveLength(1));
  });

  it("signing out this device goes to the sign-in page", async () => {
    const assign = vi.spyOn(browser, "assign").mockImplementation(() => {});
    stubFetch((url, init) => {
      if (url === "/api/auth/sessions") return [device("d1", true, CHROME)];
      if (url === "/api/auth/sessions/d1" && init?.method === "DELETE") return new Response(null, { status: 204 });
      throw new Error(`unexpected ${url}`);
    });
    renderApp("/settings/account");
    const mine = await screen.findByRole("listitem", { name: "Chrome on Linux" });
    await userEvent.setup().click(within(mine).getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/login"));
  });
});
