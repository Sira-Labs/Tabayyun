import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { browser } from "../auth";
import { ME, renderApp, stubFetch } from "./helpers";

afterEach(() => vi.restoreAllMocks());

describe("Sign out", () => {
  it("posts the logout, then navigates to the IdP's logout URL", async () => {
    const assign = vi.spyOn(browser, "assign").mockImplementation(() => {});
    const fetch = stubFetch((url, init) => {
      if (url === "/api/auth/logout" && init?.method === "POST") return { logout_url: "https://idp.example/logout?id_token_hint=t" };
      if (url.startsWith("/api/runs")) return { items: [], next_cursor: null };
      throw new Error(`unexpected ${url}`);
    });
    renderApp("/runs");

    expect(await screen.findByText(`${ME.user.display_name} · ${ME.org.name}`)).toBeTruthy();
    await userEvent.setup().click(screen.getByRole("button", { name: /Sign out/ }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("https://idp.example/logout?id_token_hint=t"));
    expect(fetch.mock.calls.some(([url, init]) => url === "/api/auth/logout" && init?.method === "POST")).toBe(true);
  });

  it("offers no sign-out in dev mode", async () => {
    stubFetch(() => ({ items: [], next_cursor: null }), { me: { ...ME, sign_in_method: "dev" } });
    renderApp("/runs");
    await screen.findByRole("heading", { name: "Runs" });
    expect(screen.queryByRole("button", { name: /Sign out/ })).toBeNull();
  });
});
