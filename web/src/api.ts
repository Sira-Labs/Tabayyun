// Thin fetch wrapper. Session cookie auth; every request carries the CSRF header
// (docs/frontend/02-security-baseline.md). Replaced by the generated OpenAPI client later.
import type { Finding, Page, Run, RunCreated, ScoreRow, Series } from "./types";

const HEADERS = { Accept: "application/json", "X-Tabayyun-Request": "1" };

/** A non-2xx answer; `message` is the server's detail when it sent one, `body` its parsed JSON. */
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
    public readonly body: unknown = null,
  ) {
    super(message);
  }
}

// Called on any 401: the router sends the user to /login with the current path (spec 013).
let onUnauthorized: (() => void) | null = null;

/** Register what happens when the session is missing or expired. */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

/** Parsed JSON of a body, or null. */
function parseJson(body: string): unknown {
  try {
    return JSON.parse(body);
  } catch {
    return null;
  }
}

type ValidationItem = { loc?: (string | number)[]; msg?: string };

/** The server's `detail` as one line: a string, or FastAPI's validation list as `field: msg`. */
export function detailText(detail: unknown): string | null {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const parts = (detail as ValidationItem[]).map((item) => {
      const field = item.loc?.filter((p) => p !== "body").join(".");
      return field ? `${field}: ${item.msg ?? "invalid"}` : (item.msg ?? "invalid");
    });
    return parts.length ? parts.join("; ") : null;
  }
  if (detail && typeof detail === "object" && "message" in detail) return String((detail as { message: unknown }).message);
  return null;
}

/** Error text from a response body: JSON `detail`, else short plain text; HTML pages keep the status. */
export function errorMessage(body: string, fallback: string): string {
  try {
    return detailText((JSON.parse(body) as { detail?: unknown }).detail) ?? fallback;
  } catch {
    const text = body.trim();
    return text && !text.startsWith("<") ? text.slice(0, 300) : fallback;
  }
}

/** Fetch JSON with the CSRF header; throws ApiError with the server's detail. A 401 also
 * triggers the unauthorized handler; 204 resolves to undefined. */
async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, { credentials: "same-origin", ...init, headers: { ...HEADERS, ...init.headers } });
  if (!res.ok) {
    const body = await res.text();
    if (res.status === 401) onUnauthorized?.();
    throw new ApiError(res.status, errorMessage(body, `${res.status} ${res.statusText}`.trim()), parseJson(body));
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** GET a JSON resource. */
export function apiGet<T>(path: string): Promise<T> {
  return request<T>(path);
}

/** Send a JSON body with an unsafe method. */
export function apiSend<T>(method: "POST" | "PUT" | "PATCH" | "DELETE", path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

/** Query string from the defined parameters, with a leading `?` or empty. */
function query(params: Record<string, string | number | undefined>): string {
  const q = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value !== undefined) q.set(key, String(value));
  const s = q.toString();
  return s ? `?${s}` : "";
}

// Upper bound on pages followed for one list, so a server bug cannot loop the client forever.
const MAX_PAGES = 50;

/** Every item of a keyset-paginated list, following `next_cursor`. */
export async function allPages<T>(url: (cursor?: string) => string): Promise<Page<T>> {
  const items: T[] = [];
  let cursor: string | undefined;
  for (let i = 0; i < MAX_PAGES; i++) {
    const page = await apiGet<Page<T>>(url(cursor));
    items.push(...page.items);
    if (!page.next_cursor) return { items, next_cursor: null };
    cursor = page.next_cursor;
  }
  return { items, next_cursor: cursor ?? null };
}

export const api = {
  createRun: (form: FormData) => request<RunCreated>("/api/runs", { method: "POST", body: form }),
  getRun: (id: string) => apiGet<Run>(`/api/runs/${encodeURIComponent(id)}`),
  listRuns: (cursor?: string, limit = 25) => apiGet<Page<Run>>(`/api/runs${query({ cursor, limit })}`),
  listRunFindings: (runId: string) =>
    allPages<Finding>((cursor) => `/api/findings${query({ run_id: runId, status: "all", limit: 500, cursor })}`),
  getSeries: (id: string) => apiGet<Series>(`/api/series/${encodeURIComponent(id)}`),
  runScores: (seriesId: string, runId: string) =>
    apiGet<Page<ScoreRow>>(`/api/series/${encodeURIComponent(seriesId)}/scores${query({ run_id: runId, limit: 10 })}`),
};
