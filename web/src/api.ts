// Thin fetch wrapper. Session cookie auth; every request carries the CSRF header
// (docs/frontend/02-security-baseline.md). Replaced by the generated OpenAPI client later.
import type { Finding, Page, Run, RunCreated, ScoreRow, Series } from "./types";

const HEADERS = { Accept: "application/json", "X-Tabayyun-Request": "1" };

/** A non-2xx answer; `message` is the server's detail when it sent one. */
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
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

/** Fetch JSON with the CSRF header; throws ApiError with the server's detail. */
async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, { credentials: "same-origin", ...init, headers: { ...HEADERS, ...init.headers } });
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`.trim();
    try {
      message = detailText(((await res.json()) as { detail?: unknown }).detail) ?? message;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, message);
  }
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

export const api = {
  createRun: (form: FormData) => request<RunCreated>("/api/runs", { method: "POST", body: form }),
  getRun: (id: string) => apiGet<Run>(`/api/runs/${encodeURIComponent(id)}`),
  listRuns: (cursor?: string, limit = 25) => apiGet<Page<Run>>(`/api/runs${query({ cursor, limit })}`),
  listRunFindings: (runId: string) =>
    apiGet<Page<Finding>>(`/api/findings${query({ run_id: runId, status: "all", limit: 500 })}`),
  getSeries: (id: string) => apiGet<Series>(`/api/series/${encodeURIComponent(id)}`),
  listScores: (seriesId: string) => apiGet<Page<ScoreRow>>(`/api/series/${encodeURIComponent(seriesId)}/scores${query({ limit: 100 })}`),
};
