// Check suites (spec 027): types and API calls of the suites pages.
import { ApiError, allPages, apiGet, apiSend } from "./api";
import type { Page, Run, RunCreated, RunStatus } from "./types";

export type CheckScope = "series" | "group";
export type ThresholdSource = "override" | "default" | "metadata" | "baseline";

/** One built-in check of the catalogue: its default parameters, and for those decided at run
 * time where the value comes from. */
export type CatalogueCheck = {
  id: string;
  scope: CheckScope;
  defaults: Record<string, unknown>;
  auto: Record<string, { source: "metadata" | "baseline"; note: string }>;
};

/** The value that applies for one parameter of a suite's check, and why. */
export type Threshold = { param: string; value: unknown; source: ThresholdSource; note: string | null };

export type SuiteCheck = {
  id: string;
  scope: CheckScope;
  enabled: boolean;
  params: Record<string, unknown>;
  thresholds: Threshold[];
};

export type Suite = {
  id: string;
  name: string;
  dataset_id: string | null;
  dataset_name: string | null;
  schedule: string | null;
  enabled: boolean;
  checks: SuiteCheck[];
  next_run_at: string | null;
  last_run_at: string | null;
  last_run: { id: string; status: RunStatus; created_at: string } | null;
  created_at: string;
  updated_at: string;
};

/** A check as the API takes it. */
export type SuiteCheckInput = { id: string; enabled: boolean; params: Record<string, unknown> };

/** What the suite form gives: every field of a suite a user sets. */
export type SuiteInput = {
  name: string;
  dataset_id: string;
  schedule: string | null;
  enabled: boolean;
  checks: SuiteCheckInput[];
};

/** One row of `GET /api/datasets` (spec 008). */
export type Dataset = {
  id: string;
  name: string;
  series: { id: string; external_id: string }[];
  window: Record<string, string>;
  created_at: string;
};

/** The fields of `input` that differ from the stored suite: the body of a PATCH. */
export function suitePatch(suite: Suite, input: SuiteInput): Partial<SuiteInput> {
  const patch: Partial<SuiteInput> = {};
  if (input.name !== suite.name) patch.name = input.name;
  if (input.dataset_id !== suite.dataset_id) patch.dataset_id = input.dataset_id;
  if (input.schedule !== suite.schedule) patch.schedule = input.schedule;
  if (input.enabled !== suite.enabled) patch.enabled = input.enabled;
  const stored = suite.checks.map(({ id, enabled, params }) => ({ id, enabled, params }));
  if (JSON.stringify(stored) !== JSON.stringify(input.checks)) patch.checks = input.checks;
  return patch;
}

export const SUITE_FIELDS = ["name", "dataset_id", "schedule", "checks"] as const;
export type SuiteField = (typeof SUITE_FIELDS)[number];

/** A failed create or update, sorted by the form field it belongs to; `other` is what belongs
 * to none. */
export type SuiteErrors = { fields: Partial<Record<SuiteField, string>>; other: string | null };

type ValidationItem = { loc?: (string | number)[]; msg?: string };

/** The API's answer to a refused suite, by field: a 422 names its fields, a 409 is the name
 * that is taken. */
export function suiteErrors(error: unknown): SuiteErrors {
  const out: SuiteErrors = { fields: {}, other: null };
  if (!error) return out;
  if (!(error instanceof ApiError)) {
    out.other = error instanceof Error ? error.message : String(error);
    return out;
  }
  if (error.status === 409) {
    out.fields.name = "A suite with this name already exists.";
    return out;
  }
  const detail = (error.body as { detail?: unknown } | null)?.detail;
  if (error.status !== 422 || !Array.isArray(detail)) {
    out.other = error.status === 403 ? "You need the editor role to change suites." : error.message;
    return out;
  }
  const rest: string[] = [];
  for (const item of detail as ValidationItem[]) {
    const [field, ...inner] = (item.loc ?? []).filter((p) => p !== "body");
    const msg = item.msg ?? "invalid";
    const line = inner.length ? `${inner.join(".")}: ${msg}` : msg;
    if ((SUITE_FIELDS as readonly unknown[]).includes(field)) {
      const key = field as SuiteField;
      out.fields[key] = out.fields[key] ? `${out.fields[key]}; ${line}` : line;
    } else {
      rest.push(field === undefined ? msg : `${String(field)}: ${line}`);
    }
  }
  if (rest.length) out.other = rest.join("; ");
  return out;
}

const base = (id: string) => `/api/suites/${encodeURIComponent(id)}`;

/** Query string of a page of a list. */
function page(params: Record<string, string | number | undefined>): string {
  const q = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value !== undefined) q.set(key, String(value));
  return q.toString();
}

export const suitesApi = {
  list: (cursor?: string, limit = 100) => apiGet<Page<Suite>>(`/api/suites?${page({ limit, cursor })}`),
  /** Every suite of the workspace, by name. */
  listAll: () => allPages<Suite>((cursor) => `/api/suites?${page({ limit: 100, cursor })}`),
  get: (id: string) => apiGet<Suite>(base(id)),
  create: (body: SuiteInput) => apiSend<Suite>("POST", "/api/suites", body),
  update: (id: string, body: Partial<SuiteInput>) => apiSend<Suite>("PATCH", base(id), body),
  remove: (id: string) => apiSend<void>("DELETE", base(id)),
  runNow: (id: string) => apiSend<RunCreated>("POST", `${base(id)}/runs`),
  catalogue: () => apiGet<CatalogueCheck[]>("/api/checks/catalogue"),
  /** Every dataset of the workspace, for the form's menu. */
  datasets: () => allPages<Dataset>((cursor) => `/api/datasets?${page({ limit: 100, cursor })}`),
  runs: (id: string, cursor?: string, limit = 25) =>
    apiGet<Page<Run>>(`/api/runs?${page({ suite_id: id, limit, cursor })}`),
};
