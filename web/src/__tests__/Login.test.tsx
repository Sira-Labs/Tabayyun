import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { json, renderApp, stubFetch } from "./helpers";

describe("Login", () => {
  it("shows one button per enabled sign-in method, each carrying next", async () => {
    stubFetch((url) => {
      if (url === "/api/auth/sign-in-options") return { google: true, github: false, passkey: true };
      throw new Error(`unexpected ${url}`);
    });
    renderApp("/login?next=%2Fruns%2Fabc");

    const google = await screen.findByRole("link", { name: "Continue with Google" });
    expect(google.getAttribute("href")).toBe("/api/auth/login?method=google&next=%2Fruns%2Fabc");
    expect(screen.getByRole("link", { name: "Sign in with a passkey" }).getAttribute("href")).toBe(
      "/api/auth/login?method=passkey&next=%2Fruns%2Fabc",
    );
    expect(screen.queryByRole("link", { name: "Continue with GitHub" })).toBeNull();
  });

  it("drops a next that leaves the site", async () => {
    stubFetch(() => ({ google: true, github: true, passkey: true }));
    renderApp("/login?next=%2F%2Fevil.example");
    const github = await screen.findByRole("link", { name: "Continue with GitHub" });
    expect(github.getAttribute("href")).toBe("/api/auth/login?method=github&next=%2F");
  });

  it("sends a signed-out user to /login with the current path", async () => {
    stubFetch(
      (url) => {
        if (url === "/api/auth/sign-in-options") return { google: true, github: true, passkey: true };
        return json({ detail: "not_authenticated" }, 401);
      },
      { me: json({ detail: "not_authenticated" }, 401) },
    );
    const { router } = renderApp("/runs?cursor=c1");

    await screen.findByRole("heading", { name: "Sign in" });
    await waitFor(() => expect(router.state.location.pathname).toBe("/login"));
    expect(router.state.location.search).toEqual({ next: "/runs?cursor=c1" });
  });

  it("explains dev mode when no method is enabled", async () => {
    stubFetch(() => ({ google: false, github: false, passkey: false }));
    renderApp("/login");
    expect(await screen.findByText(/runs without sign-in/)).toBeTruthy();
  });
});
