// Connector sources and their jobs (specs 021–024): types and API calls of the sources pages.
import { apiGet, apiSend } from "./api";
import type { Page, SeriesSummary } from "./types";

export type HealthStatus = "ok" | "degraded" | "failing" | "unknown";
export type JobStatus = "queued" | "running" | "succeeded" | "partial" | "failed";
export type JobTrigger = "manual" | "poll" | "run" | "check" | "search" | "metadata";

/** Connector types the pages know forms for; others are shown read-only. */
export const CONNECTOR_TYPES = ["pi_web_api", "opc_ua", "synthetic"] as const;
export type ConnectorType = (typeof CONNECTOR_TYPES)[number];
export const TYPE_LABEL: Record<string, string> = {
  pi_web_api: "PI Web API",
  opc_ua: "OPC UA",
  synthetic: "Synthetic",
  upload: "Uploads",
  csv_dir: "CSV directory",
};

export type SourceSummary = {
  id: string;
  type: string;
  name: string;
  n_series: number;
  created_at: string;
  enabled: boolean;
  connector: boolean;
  health_status: HealthStatus;
  last_fetch?: { id: string; status: JobStatus; finished_at: string; rows: number } | null;
};

export type Health = {
  status?: HealthStatus;
  checked_at?: string;
  last_success_at?: string;
  consecutive_failures?: number;
  last_error?: { at: string; message: string | null; retryable: boolean };
  last_fetch?: { id: string; status: JobStatus; finished_at: string; rows: number };
};

export type SourceDetail = {
  id: string;
  type: string;
  name: string;
  enabled: boolean;
  connector: boolean;
  config: Record<string, unknown>;
  health: Health;
  credentials: { set: boolean; updated_at: string | null };
  n_series: number;
  polled_at: string | null;
  created_at: string;
  updated_at: string;
};

export type Job = {
  id: string;
  trigger: JobTrigger;
  status: JobStatus;
  window_start: string;
  window_end: string;
  series_ids: string[] | null;
  force: boolean;
  calls: number;
  rows: number;
  error: string | null;
  requested_by: string | null;
  run_id: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

export type SearchItem = { external_id: string; name: string; unit: string | null; description: string | null };
export type SeriesChange = { changed?: string[]; skipped?: string[]; error?: string };
export type JobResult = {
  items?: SearchItem[];
  truncated?: boolean;
  updated?: number;
  unchanged?: number;
  failed?: number;
  series?: Record<string, SeriesChange>;
  point_errors?: Record<string, string>;
};
export type JobDetail = Job & { params: Record<string, unknown> | null; result: JobResult | null };

export type ClientCertificate = {
  certificate_pem: string;
  sha1: string;
  sha256: string;
  application_uri: string | null;
  not_after: string;
};

export type FieldError = { loc: (string | number)[]; msg: string; type: string };

/** Whether a job is still waiting or working. */
export function isActive(status: JobStatus): boolean {
  return status === "queued" || status === "running";
}

const base = (id: string) => `/api/sources/${encodeURIComponent(id)}`;

export const sourcesApi = {
  list: () => apiGet<{ items: SourceSummary[] }>("/api/sources"),
  get: (id: string) => apiGet<SourceDetail>(base(id)),
  create: (body: { type: string; name: string; config: Record<string, unknown> }) =>
    apiSend<SourceDetail>("POST", "/api/sources", body),
  update: (id: string, body: { name?: string; config?: Record<string, unknown>; enabled?: boolean }) =>
    apiSend<SourceDetail>("PATCH", base(id), body),
  setCredentials: (id: string, body: Record<string, unknown>) => apiSend<void>("PUT", `${base(id)}/credentials`, body),
  clearCredentials: (id: string) => apiSend<void>("DELETE", `${base(id)}/credentials`),
  check: (id: string) => apiSend<{ id: string }>("POST", `${base(id)}/check`),
  fetch: (id: string, body: { start: string; end: string; force?: boolean }) =>
    apiSend<{ id: string }>("POST", `${base(id)}/fetches`, body),
  search: (id: string, query: string, limit = 100) =>
    apiSend<{ id: string }>("POST", `${base(id)}/search`, { query, limit }),
  importMetadata: (id: string, overwrite: boolean) =>
    apiSend<{ id: string }>("POST", `${base(id)}/metadata`, { overwrite }),
  registerSeries: (id: string, points: { external_id: string; name: string; unit?: string | null }[]) =>
    apiSend<{ created: number; existing: number }>("POST", `${base(id)}/series`, points),
  jobs: (id: string, limit = 20) => apiGet<{ items: Job[] }>(`${base(id)}/fetches?limit=${limit}`),
  job: (id: string, jobId: string) => apiGet<JobDetail>(`${base(id)}/fetches/${encodeURIComponent(jobId)}`),
  clientCertificate: (id: string) => apiGet<ClientCertificate>(`${base(id)}/client-certificate`),
};

export type SeriesFilters = { q?: string; source?: string; unit?: string; kind?: string; score?: number };

/** Query string of the catalogue's filters and page. */
export function seriesQuery(filters: SeriesFilters, cursor?: string, limit = 50): string {
  const q = new URLSearchParams();
  if (filters.q) q.set("q", filters.q);
  if (filters.source) q.set("source_id", filters.source);
  if (filters.unit) q.set("unit", filters.unit);
  if (filters.kind) q.set("kind", filters.kind);
  if (filters.score !== undefined) q.set("score_max", String(filters.score));
  q.set("limit", String(limit));
  if (cursor) q.set("cursor", cursor);
  return `/api/series?${q.toString()}`;
}

export const seriesApi = {
  list: (filters: SeriesFilters, cursor?: string) => apiGet<Page<SeriesSummary>>(seriesQuery(filters, cursor)),
  units: () => apiGet<{ items: { unit: string; n: number }[] }>("/api/series/units"),
};

/** The thumbprint and subject of an OPC UA "not pinned" error (spec 023), or null. */
export function unpinnedCertificate(message: string | null | undefined): { sha256: string; subject: string } | null {
  const match = /server certificate not pinned: SHA-256 ([0-9a-f]{64}), subject ([^;]*)/.exec(message ?? "");
  return match ? { sha256: match[1] as string, subject: (match[2] as string).trim() } : null;
}
