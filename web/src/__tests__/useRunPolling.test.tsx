import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";
import { useRunPolling } from "../hooks/useRunPolling";
import { makeRun, stubFetch } from "./helpers";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("useRunPolling", () => {
  it("polls while queued or running and stops when the run is terminal", async () => {
    const statuses = ["queued", "running", "succeeded"] as const;
    let calls = 0;
    const fetch = stubFetch(() => makeRun({ status: statuses[Math.min(calls++, statuses.length - 1)] }));

    const { result } = renderHook(() => useRunPolling("run-1", 20), { wrapper });

    await waitFor(() => expect(result.current.data?.status).toBe("succeeded"));
    const after = fetch.mock.calls.length;
    expect(after).toBe(3);
    await new Promise((r) => setTimeout(r, 120));
    expect(fetch.mock.calls.length).toBe(after);
  });

  it("stops on failed too", async () => {
    const fetch = stubFetch(() => makeRun({ status: "failed", error: "cannot parse CSV: bad" }));
    const { result } = renderHook(() => useRunPolling("run-2", 20), { wrapper });
    await waitFor(() => expect(result.current.data?.status).toBe("failed"));
    await new Promise((r) => setTimeout(r, 100));
    expect(fetch.mock.calls.length).toBe(1);
  });
});
