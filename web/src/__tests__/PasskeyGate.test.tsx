import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { adminRoute, adminState } from "./adminFake";
import { renderApp, stubFetch } from "./helpers";

afterEach(() => vi.restoreAllMocks());

describe("Admin: passkey gate", () => {
  it("asks for a passkey sign-in when the server requires one", async () => {
    stubFetch(adminRoute(adminState(), { gate: true }));
    renderApp("/admin");

    expect(await screen.findByRole("heading", { name: "Sign in with a passkey to continue" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Sign in with a passkey" }).getAttribute("href")).toBe(
      "/api/auth/login?method=passkey&next=%2Fadmin",
    );
    expect(screen.getByRole("link", { name: "Manage passkeys" }).getAttribute("href")).toBe("/api/auth/passkeys");
    expect(screen.queryByRole("navigation", { name: "Admin sections" })).toBeNull();
  });
});
