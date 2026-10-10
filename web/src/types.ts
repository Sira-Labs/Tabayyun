// Shapes of the API responses the web app reads (specs 002–004). Timestamps named `window`,
// `ts` or `*_ns` are nanoseconds since the epoch; `*_at` fields are RFC 3339 strings.

export type Severity = "low" | "medium" | "high" | "critical";
export type RunStatus = "queued" | "running" | "succeeded" | "failed";
export type FindingStatus = "open" | "acked" | "muted" | "resolved";
export type NsWindow = { start: number; end: number };

export type SkippedCheck = { check_id: string; missing: string };

export type RunStats = {
  n_series?: number;
  n_samples?: number;
  n_findings?: number;
  n_metrics?: number;
  n_findings_new?: number;
  n_findings_merged?: number;
  skipped?: number;
  skipped_checks?: SkippedCheck[];
  ts_unit?: string;
};

export type RunSeries = { id: string; external_id: string; score: number };

export type Run = {
  id: string;
  /** Set on a dataset run (spec 008); absent or null on an upload run. */
  dataset_id?: string | null;
  trigger: string;
  status: RunStatus;
  window: NsWindow | null;
  now_ns: number | null;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  stats: RunStats;
  series: RunSeries[];
  error: string | null;
  created_at: string;
};

export type RunCreated = { id: string; status: RunStatus; created_at: string };
export type Page<T> = { items: T[]; next_cursor: string | null };

export type Finding = {
  id: string;
  check_id: string;
  series_id: string;
  dimension: string;
  severity: Severity;
  window: NsWindow;
  score_impact: number;
  summary: string;
  evidence: Record<string, unknown>;
  status: FindingStatus;
  status_reason: string | null;
  first_run_id: string | null;
  last_run_id: string | null;
  occurrences: number;
  created_at: string;
  updated_at: string;
};

/** Where a public example series comes from (spec 019). */
export type ExampleSource = { source?: string; url?: string; licence?: string; notes?: string };

export type Series = {
  id: string;
  source_id: string;
  external_id: string;
  name: string;
  unit: string | null;
  kind: string;
  physical_min: number | null;
  physical_max: number | null;
  operational_min: number | null;
  operational_max: number | null;
  /** Declared sampling step in ns; only the detail route returns it. */
  expected_interval_ns?: number | null;
  /** Free-form metadata; `example` is set by `deploy/examples/public.py` (spec 019). */
  metadata?: { example?: ExampleSource } & Record<string, unknown>;
  latest_score: { overall: number; computed_at: string } | null;
  open_findings: number;
  n_runs: number;
  last_run_at: string | null;
};

export type ScoreRow = {
  series_id: string;
  run_id: string;
  layer: string;
  method_version: string;
  overall: number;
  dimensions: Record<string, number>;
  n_findings: number;
  computed_at: string;
};

/** One row of `GET /api/series` (spec 004, the catalogue of spec 024). */
export type SeriesSummary = {
  id: string;
  source_id: string;
  external_id: string;
  name: string;
  unit: string | null;
  kind: string;
  latest_score: { overall: number; computed_at: string } | null;
  open_findings: number;
  last_run_at: string | null;
};
