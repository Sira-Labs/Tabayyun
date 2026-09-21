export type Severity = "low" | "medium" | "high" | "critical";

export type Finding = {
  check_id: string;
  series_id: string;
  dimension: string;
  severity: Severity;
  window: { start: number; end: number };
  score_impact: number;
  summary: string;
  evidence: Record<string, unknown>;
};

export type Score = {
  series_id: string;
  method_version: string;
  overall: number;
  dimensions: Record<string, number>;
  n_findings: number;
};

export type CheckReport = {
  series_id: string;
  n_samples: number;
  window: { start: number; end: number };
  now_ns: number;
  score: Score;
  findings: Finding[];
  metrics: { check_id: string; name: string; ts: number; value: number }[];
  skipped: { check_id: string; missing: string }[];
  profile: Record<string, unknown> | null;
};
