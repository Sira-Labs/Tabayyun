import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { browser } from "../auth";
import { json, renderApp, stubFetch } from "./helpers";

afterEach(() => vi.restoreAllMocks());

describe("NoAccess", () => {
  it("shows 'No access yet' with the signed-in email, and signs out", async () => {
    const assign = vi.spyOn(browser, "assign").mockImplementation(() => {});
    const fetch = stubFetch(
      (url, init) => {
        if (url === "/api/auth/logout" && init?.method === "POST") return { logout_url: "https://idp.example/logout?x=1" };
        throw new Error(`unexpected ${url}`);
      },
      { me: json({ detail: "no_access", email: "bo@example.org" }, 403) },
    );
    renderApp("/runs");

    expect(await screen.findByRole("heading", { name: "No access yet" })).toBeTruthy();
    expect(screen.getByText("bo@example.org")).toBeTruthy();
    expect(screen.queryByRole("navigation", { name: "Main" })).toBeNull();

    await userEvent.setup().click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("https://idp.example/logout?x=1"));
    const logout = fetch.mock.calls.find(([url]) => url === "/api/auth/logout");
    expect((logout?.[1]?.headers as Record<string, string>)["X-Tabayyun-Request"]).toBe("1");
  });
});
