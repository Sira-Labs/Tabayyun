import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory } from "@tanstack/react-router";
import { render } from "@testing-library/react";
import { vi } from "vitest";
import { makeRouter } from "../router";
import type { Run } from "../types";

export type Route = (url: string, init?: RequestInit) => unknown | Promise<unknown>;

/** JSON response helper. */
export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

/** Stub `fetch` with a router function; a returned Response is passed through, anything else is JSON. */
export function stubFetch(route: Route) {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.pathname + input.search : input.url;
    if (url.startsWith("/api/version")) return json({ version: "0.1.0", env: "test", schema_revision: "0002" });
    const out = await route(url, init);
    return out instanceof Response ? out : json(out);
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

/** Render the app at `path` with a fresh query client and memory history. */
export function renderApp(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = makeRouter(createMemoryHistory({ initialEntries: [path] }));
  const view = render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { ...view, router, client };
}

/** A run with sensible defaults. */
export function makeRun(overrides: Partial<Run> = {}): Run {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    trigger: "upload",
    status: "succeeded",
    window: { start: 1_767_225_600_000_000_000, end: 1_767_484_800_000_000_000 },
    now_ns: 1_767_484_800_000_000_000,
    started_at: "2026-01-01T00:00:00Z",
    finished_at: "2026-01-01T00:00:01Z",
    duration_ms: 950,
    stats: { n_samples: 72, n_findings: 0, skipped_checks: [] },
    series: [{ id: "22222222-2222-2222-2222-222222222222", external_id: "pump-1", score: 97.5 }],
    error: null,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}
