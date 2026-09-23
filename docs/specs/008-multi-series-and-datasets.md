# Spec 008 — Multi-series checks, series groups and dataset runs

Sprint 7, stories S7-3 (M) and S7-8 (S, raised from C on 2026-09-23 so cross-series checks
reach the live system). Depends on: 006 (cache), 003 (findings), 004 (series). Packages:
`core/tabayyun-core/src/{align.rs,cross.rs,registry.rs}`, `core/tabayyun-py`,
`core/tabayyun-cli`, `api/src/tabayyun/db/migrations` (0003),
`api/src/tabayyun/services/{groups,datasets,runs}.py`, `api/src/tabayyun/routers/{groups,datasets,runs}.py`.

## Goal

Checks can look at several series at once. A *series group* names the series that belong
together and how (related, redundant, balance with inputs and outputs); a *dataset* names the
series and the time window a run covers. `POST /api/runs` with a dataset reads the series
from the cache, runs the single-series checks on each and the cross-series checks on every
group whose members are all in the dataset, and persists findings and scores per series.
Specs 009–011 add the three cross-series checks on this frame.

## User story

As a process engineer, I declare once that PT-101A/B/C measure the same pressure and that
the inlet and outlet meters form a balance, then run a dataset over last week and see which
sensor disagrees or where the balance does not close.

## Interface

Rust:

```rust
pub enum GroupKind { Related, Redundant, Balance }
pub enum MemberRole { Member, Input, Output }
pub struct GroupMember { pub series_id: String, pub role: MemberRole }
pub struct SeriesGroup { pub id: String, pub name: String, pub kind: GroupKind,
                         pub members: Vec<GroupMember>, pub params: serde_json::Value }

pub trait CrossCheck: Send + Sync {
    fn id(&self) -> &'static str;
    fn dimension(&self) -> Dimension;
    fn default_severity(&self) -> Severity;
    fn kinds(&self) -> &'static [GroupKind];        // groups it applies to
    fn run(&self, frames: &[&SeriesFrame], group: &SeriesGroup, ctx: &CheckContext)
        -> Result<CheckOutput>;
}

// align.rs: members on one grid, bin = coarsest expected interval among members
// (or params.grid); value per bin = mean of usable samples, NaN when none.
pub struct Aligned { pub ts: Vec<i64>, pub grid_ns: i64, pub columns: Vec<Vec<f64>> }
pub fn align(frames: &[&SeriesFrame], grid_ns: Option<i64>) -> Aligned;

impl Registry {
    pub fn run_multi(configs: &[CheckConfig], frames: &[SeriesFrame], groups: &[SeriesGroup],
                     ctx: &CheckContext) -> Result<MultiOutput>;
}
pub struct MultiOutput {
    pub per_series: BTreeMap<String, CheckOutput>,     // cross findings land here too
    pub groups_skipped: Vec<GroupSkip>,                // feeds stats.groups_skipped
}
pub struct GroupSkip { pub group_id: String, pub reason: String, pub missing: Vec<String> }
```

Cross-series findings name one series in `series_id`: the suspect when the check can tell
(voting, measurement test), otherwise the group's first member. Their evidence always holds
`group_id`, `group_name` and `members` (series ids), so the finding shape stays stable
(ADR-0013 dedup keys on the evidence key set).

Bindings: `run_checks_multi(tables: dict[str, table], metas: dict[str, dict],
groups: list[dict], configs: list[dict] | None, now_ns: int | None) -> dict` returning one
report per series (the existing report shape) plus `groups_skipped`.

CLI: `tabayyun check-multi <file> --value-cols a,b,c --groups groups.json`
(wide CSV/Parquet, one series per value column).

Database (migration 0003):

```
series_groups        id uuid pk, org_id, workspace_id, name text, kind text
                     CHECK (kind in related, redundant, balance), params jsonb default {},
                     created_at, updated_at; unique (workspace_id, name)
series_group_members group_id fk → series_groups on delete cascade, series_id fk → series,
                     role text CHECK (role in member, input, output), position int;
                     pk (group_id, series_id)
```

`datasets` and `dataset_series` exist since spec 001; `datasets.window_policy` holds
`{"start": iso, "end": iso}` or `{"last": "7d"}`.

Routes:

```
POST   /api/series-groups   {name, kind, members: [{series_id, role}], params?} → 201 Group
GET    /api/series-groups?series_id                                          → 200 {items}
GET    /api/series-groups/{id}                                               → 200 Group
PATCH  /api/series-groups/{id}  any of name, members, params                 → 200 Group
DELETE /api/series-groups/{id}                                               → 204
POST   /api/datasets        {name, series_ids: [...], window: {start,end} | {last}} → 201 Dataset
GET    /api/datasets, GET/PATCH/DELETE /api/datasets/{id}
POST   /api/runs            JSON {dataset_id, now?}  → 202 {run_id, status: "queued"}
                            (multipart upload unchanged)
```

## Behaviour

1. Group validation (422 naming the field): 2–32 members; each series exists in the
   workspace; `redundant` and `related` members all have role `member`; `balance` has at
   least one `input` and one `output` and no `member`; no duplicate series; `params` is a JSON
   object ≤ 8 KiB. Name unique per workspace (409 on conflict).
2. Dataset validation: 1–500 series, all existing; window either both `start < end` or a
   `last` duration from 1 h to 400 d. Deleting a dataset keeps its past runs (run keeps
   `dataset_id`, the FK becomes `ON DELETE SET NULL` in 0003).
3. `POST /api/runs {dataset_id}` creates a run with `trigger = "manual"`, `dataset_id` set,
   resolves `last` against `now` (or the request's `now`), stores the resolved window in
   `stats.window`, and enqueues it (inline mode as for uploads). Unknown dataset → 404.
4. The worker reads each series from the cache for the window, computes the missing ranges
   per series (spec 006) and records them in `stats.missing` (`{series_id: [[s, e], ...]}`).
   A series with no rows is listed in `stats.series_skipped` with reason `no cached data`
   and left out; if every series is empty the run fails with `no cached data in window`.
5. The worker runs `run_multi` with every enabled single-series check and every cross check
   whose `kinds()` includes the group's kind, for each group all of whose members have data.
   Groups with a member lacking data are listed in `groups_skipped` with the missing ids.
6. Persistence reuses spec 003 per series (scores, metrics, findings with dedup); a
   cross-series finding merges only with earlier findings of the same check, series and
   evidence shape, so findings from different groups never merge (group ids differ in
   evidence values, not keys, so dedup also requires equal `group_id`; see step 7).
7. Dedup rule addition (recorded in ADR-0013 as an amendment): when the incoming evidence has
   a `group_id`, a candidate must have the same `group_id`.
8. `stats` of a dataset run: `n_series`, `n_samples`, `n_findings`, `n_findings_new`,
   `n_findings_merged`, `series` (per series id, external id, score), `groups` (ids run),
   `groups_skipped`, `series_skipped`, `missing`, `window`.
9. `align` never invents data: a bin with no usable sample is NaN, and cross checks ignore
   bins where any required member is NaN.

## Acceptance criteria

- [x] `align` unit tests: common grid from the coarsest interval, mean per bin, NaN for
      empty bins, members with offset timestamps line up.
- [x] `run_multi` runs single-series checks per frame and a fake cross check per matching
      group; a group with a missing member is skipped and reported.
- [x] Bindings round trip: three pyarrow tables and one group produce three reports.
- [x] CLI `check-multi` on a synthetic wide CSV prints per-series findings.
- [x] Migration 0003 upgrades and downgrades cleanly; `alembic check` is clean.
- [x] Group and dataset routes pass their validation tests (every 422 and 409 path above).
- [x] A dataset run over two cached uploads produces per-series scores and findings, with
      `stats.missing` and `groups_skipped` filled correctly; rerunning does not double findings.
- [x] On the live system: two uploaded series, a group and a dataset produce a dataset run
      with cross-series findings (after specs 009–010 land). (Plumbing verified 2026-09-23
      after PR #32, release run 32, schema 0003: `live-008-a` and `live-008-b`, a redundant
      group and a three-day dataset gave a dataset run on the CapRover worker that read both
      series from RustFS and succeeded with the group in `stats.groups`, nothing skipped and
      only the hour after the data in `stats.missing`; no findings on the clean data. Cross-series
      finding verified 2026-09-23 after PR #33, release run 33: see spec 009, "Live check".)

## Test cases

Unit (`tabayyun-core`): `align::tests::*`, `registry::tests::run_multi_*`.
Bindings: `test_run_checks_multi`.
API: `tests/db/test_groups_api.py`, `tests/db/test_datasets_api.py`,
`tests/db/test_dataset_runs.py` (uses the local cache store in a temp dir).

## Implementation edits

Recorded on 2026-09-23 while implementing; the interface above is otherwise as built.

- `Registry::run_multi` takes a `profiles` map (series id → `Profile`). Several single-series
  checks behave differently with a profile in the context (flatline, spikes, changepoint and
  the evidence `source`), and upload runs pass each series' own profile; without the map a
  dataset run would report different findings than an upload of the same data, and dedup
  would miss merges. `run_multi_with` takes the cross checks as an argument (the built-in list
  is empty until specs 009–011), which is how the tests inject a fake cross check.
- `SeriesGroup::validate` holds the structural rules (2–32 distinct members, roles per kind,
  params an object); the API repeats them with field names for 422 responses and adds the
  workspace existence check.
- Bindings: `run_checks_multi(tables, metas, groups, configs, *, now_ns, window,
  compute_profile, ts_col, value_col, quality_col, ingest_col)` returns
  `{"reports": {series_id: report}, "groups_skipped": [...]}`. `window` makes completeness
  and scores relative to the dataset window rather than the data's extent.
- CLI: `check-multi` also takes `--metas metas.json` (series id → metadata) and `--now`.
- `POST /api/runs` with JSON answers with the upload shape of spec 002,
  `{id, status: "queued", created_at}`, rather than `{run_id, status}`, so clients handle one
  shape. The body's `now` is RFC 3339 or epoch ns. Run responses gain `dataset_id`; a dataset
  run's trigger is `manual`.
- A group with a member outside the dataset is listed in `groups_skipped` with reason
  `members not in dataset` (the spec only named members without data); silently leaving it
  out would hide why a group produced nothing.
- `stats.skipped_checks` entries of a dataset run carry `series_id`. `stats.missing` lists only
  series with gaps; a write covers `[first sample, last sample + 1 ns)` rounded up to the
  microsecond, so the gap after hourly data starts 1 µs after the last sample.
- Series are keyed by their UUID inside a dataset run (frames, groups, reports); the series
  name stays in the metadata. The ingest column is used only when every cached series has
  one, because the binding takes one column name for all tables.
- A cache read failure fails a dataset run (`cache read failed: …`), unlike an upload's cache
  write: without the read there is no data.
- List endpoints page like the others (`limit`, `cursor`, `next_cursor`). `last` accepts
  `m`, `h`, `d` and `w` units. A group's kind cannot be changed by PATCH (its members'
  roles depend on it).
- The worker job dispatches on the run's trigger (`services/execution.py`); dataset runs live
  in `services/dataset_runs.py`, which imports `services.runs` as a module to keep the
  `runs → jobs → tasks → execution` import cycle harmless.

## Out of scope

- Web screens for groups and datasets (sprint 9, with the series pages).
- Automatic related-series suggestions from correlation over the cache (catalogue check 22
  "auto-suggested"): deferred to sprint 10 with the baseline profile job (S10-2).
- Query-based dataset selection (`selection` by source, unit or asset path): sprint 9 with
  connectors; this spec stores explicit series lists only.
- Suites and schedules (S10-1).
