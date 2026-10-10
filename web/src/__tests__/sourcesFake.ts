// Fixtures of the sources pages' tests (spec 024).
import type { Me } from "../auth";
import type { Job, JobDetail, SourceDetail, SourceSummary } from "../sources";
import type { Workspace } from "../workspace";
import { ME, WORKSPACE } from "./helpers";

export const PI_ID = "aaaaaaaa-0000-0000-0000-000000000001";
export const OPC_ID = "aaaaaaaa-0000-0000-0000-000000000002";
export const SHA = "ab".repeat(32);

export const MEMBER: Me = { ...ME, role: "member" };
export const asRole = (role: Workspace["role"]): { me: Me; workspaces: Workspace[] } => ({
  me: MEMBER,
  workspaces: [{ ...WORKSPACE, role }],
});

export function summary(overrides: Partial<SourceSummary> = {}): SourceSummary {
  return {
    id: PI_ID,
    type: "pi_web_api",
    name: "PI plant",
    n_series: 2,
    created_at: "2026-10-01T00:00:00Z",
    enabled: true,
    connector: true,
    health_status: "ok",
    last_fetch: { id: "f1", status: "succeeded", finished_at: "2026-10-09T08:00:00Z", rows: 1440 },
    ...overrides,
  };
}

export function detail(overrides: Partial<SourceDetail> = {}): SourceDetail {
  return {
    id: PI_ID,
    type: "pi_web_api",
    name: "PI plant",
    enabled: true,
    connector: true,
    config: { base_url: "https://pi.example.com/piwebapi", data_server: "\\\\PISRV01", settle_s: 600 },
    health: { status: "ok", last_success_at: "2026-10-09T08:00:00Z" },
    credentials: { set: true, updated_at: "2026-10-01T00:00:00Z" },
    n_series: 2,
    polled_at: null,
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    ...overrides,
  };
}

export function job(overrides: Partial<JobDetail> = {}): JobDetail {
  return {
    id: "j1",
    trigger: "check",
    status: "succeeded",
    window_start: "2026-10-09T08:00:00Z",
    window_end: "2026-10-09T08:00:00Z",
    series_ids: null,
    force: false,
    calls: 0,
    rows: 0,
    error: null,
    requested_by: "u1",
    run_id: null,
    created_at: "2026-10-09T08:00:00Z",
    started_at: "2026-10-09T08:00:01Z",
    finished_at: "2026-10-09T08:00:02Z",
    params: null,
    result: null,
    ...overrides,
  };
}

export const history = (...jobs: Job[]) => ({ items: jobs });
