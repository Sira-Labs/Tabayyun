import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, allPages, api, errorMessage } from "../api";
import { json, stubFetch } from "./helpers";

afterEach(() => vi.unstubAllGlobals());

describe("errorMessage", () => {
  it("uses JSON detail, short plain text, and the fallback for HTML or empty bodies", () => {
    expect(errorMessage(JSON.stringify({ detail: "series not found" }), "404")).toBe("series not found");
    expect(errorMessage(JSON.stringify({ detail: [{ loc: ["body", "file"], msg: "required" }] }), "422")).toBe("file: required");
    expect(errorMessage("upstream timed out\n", "504 Gateway Timeout")).toBe("upstream timed out");
    expect(errorMessage("<html><body>Bad gateway</body></html>", "502 Bad Gateway")).toBe("502 Bad Gateway");
    expect(errorMessage("", "500")).toBe("500");
  });

  it("surfaces a plain-text error body through ApiError", async () => {
    stubFetch(() => new Response("proxy refused", { status: 502, statusText: "Bad Gateway" }));
    const err = await api.getRun("r").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(502);
    expect((err as ApiError).message).toBe("proxy refused");
  });
});

describe("allPages", () => {
  it("follows next_cursor until the last page", async () => {
    const fetchFn = stubFetch((url) => {
      const cursor = new URL(url, "http://x").searchParams.get("cursor");
      if (!cursor) return { items: [1, 2], next_cursor: "c1" };
      if (cursor === "c1") return { items: [3], next_cursor: "c2" };
      return json({ items: [4], next_cursor: null });
    });
    const page = await allPages<number>((cursor) => `/api/findings${cursor ? `?cursor=${cursor}` : ""}`);
    expect(page).toEqual({ items: [1, 2, 3, 4], next_cursor: null });
    expect(fetchFn).toHaveBeenCalledTimes(3);
  });

  it("asks for every finding of the run across statuses", async () => {
    const fetchFn = stubFetch(() => ({ items: [], next_cursor: null }));
    await api.listRunFindings("run-1");
    const url = new URL(fetchFn.mock.calls[0]![0] as string, "http://x");
    expect(Object.fromEntries(url.searchParams)).toEqual({ run_id: "run-1", status: "all", limit: "500" });
  });
});
