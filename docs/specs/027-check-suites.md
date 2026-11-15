# Spec 027 — Check suites: scheduled runs with their own check configuration

Sprint 10, story S10-1. Depends on:
- spec 008: datasets, their window policy and dataset runs;
- spec 002: the job queue and its periodic tasks;
- spec 007: tenancy, roles and row-level security;
- spec 014: the audit log.

Packages: `core/tabayyun-core` (check catalogue), `core/tabayyun-py` (bindings),
`api/src/tabayyun/` (model, migration 0012, service, router, periodic task, dataset-run
executor), `web/src/` (suites pages).

## Goal

A workspace has check suites. A suite names a dataset, the checks to run on it with their
parameters, and optionally a cron schedule. A scheduled suite produces runs without a user:
every minute the worker starts a run for each suite that is due. The suite's page shows, for
every threshold, where its value comes from (the suite's override, the check's default, the
series' metadata or its baseline), and the suite's run history.

## User story

As a data steward, I set up "Feeders, hourly" once: the feeder dataset, every check, the
spike check a little less sensitive, at five past every hour. From then on I find the runs
and their findings in the morning, without starting anything myself, and when someone asks
why a spike was not flagged I can see that the suite overrides `threshold` to 6.

## Interface

### Core

- `Registry::catalogue() -> Vec<CheckInfo>`: one entry per built-in check, single-series
  first, in registry order.
  - `id`;
  - `scope`: `series` or `group`;
  - `defaults`: the check's default parameters as a JSON object (its `Default` serialised);
  - `auto`: for each parameter whose default is not a fixed number, where the value comes
    from at run time: `{param: {source: "metadata" | "baseline", note}}`.
- `Registry::validate(configs) -> Result<()>`: builds every configured check, so an unknown
  id or parameters of the wrong type are `UnknownCheck` or `InvalidParams`.
- Bindings: `tabayyun_core.check_catalogue() -> list[dict]` and
  `tabayyun_core.validate_configs(configs) -> None` (raises `ValueError`).

### Database (migration 0012)

`check_suites`, a tenant table with row-level security by `org_id`:

| Column | Type | Meaning |
|---|---|---|
| `id` | uuid | primary key |
| `org_id`, `workspace_id` | uuid | tenancy |
| `name` | text | unique per workspace |
| `dataset_id` | uuid, null | the dataset; set null when the dataset is deleted |
| `schedule` | text, null | five-field cron in UTC; null means the suite only runs on request |
| `enabled` | boolean | a disabled suite is never started by the schedule |
| `checks` | jsonb | list of `{id, enabled, params}` in the order given |
| `next_run_at` | timestamptz, null | when the schedule fires next; null without a schedule |
| `last_run_at` | timestamptz, null | when the suite last started a run |
| `created_at`, `updated_at` | timestamptz | |

`runs` gains `suite_id` (uuid, null, set null when the suite is deleted) and `checks` (jsonb,
null): the suite's checks as they were when the run was created.

`tabayyun_claim_due_suites(now timestamptz)` is a SECURITY DEFINER function, like the one
that claims due sources: the scheduler crosses orgs. It returns `(suite_id, org_id,
workspace_id)` of every enabled suite with a schedule, a dataset and `next_run_at <= now`,
and moves their `next_run_at` ten minutes ahead as a lease.

### API

All under `/api/suites`. Reading needs the viewer role, writing and starting a run the editor
role. Ids of another workspace are 404.

- `GET /api/checks/catalogue` → `[{id, scope, defaults, auto}]` (the core's catalogue).
- `POST /api/suites` → 201. Body: `{name, dataset_id, schedule?, enabled?, checks?}`.
  - `checks` is `[{id, enabled?, params?}]`. Left out, the suite gets every built-in check,
    enabled, without overrides. Checks that are not listed do not run.
  - `enabled` defaults to true.
- `GET /api/suites?limit=&cursor=` → `{items, next_cursor}`, newest first (the keyset of the
  other lists; the web list sorts by name).
- `GET /api/suites/{id}`
- `PATCH /api/suites/{id}`: any of `name`, `dataset_id`, `schedule` (null removes it),
  `enabled`, `checks` (replaces the list).
- `DELETE /api/suites/{id}` → 204. Its runs stay, with `suite_id` null.
- `POST /api/suites/{id}/runs` → 202 `{id, status, created_at}`: starts a run now.
- `GET /api/runs?suite_id=<id>` lists a suite's runs; every run carries `suite_id`.

A suite in a response:

```
{id, name, dataset_id, dataset_name, schedule, enabled,
 checks: [{id, scope, enabled, params,
           thresholds: [{param, value, source, note}]}],
 next_run_at, last_run_at,
 last_run: {id, status, created_at} | null,
 created_at, updated_at}
```

`thresholds` lists every parameter of the check with the value that applies:
- `override`: the suite sets it (`value` is the suite's value);
- `default`: the check's fixed default;
- `metadata` or `baseline`: the default is "decide at run time", from the series' metadata
  (physical limits, resolution) or from its baseline profile; `value` is null and `note`
  says what is used.

### Web

- `/suites`: the suites with dataset, schedule in words, next run, last run and its status.
- `/suites/new` and `/suites/:id/edit` (editor): name, dataset, schedule (presets and a cron
  field), enabled, and per check an on/off switch and its parameters.
- `/suites/:id`: the configuration, every threshold with its source, "Run now", enable or
  disable, delete, and the run history with links to the run reports.
- "Suites" in the navigation.

## Behaviour

1. **Validation** (422 with the field named):
   - `name`: 1 to 256 characters, unique in the workspace (409 on a clash);
   - `dataset_id`: a dataset of the workspace;
   - `schedule`: five cron fields that parse; successive firings must be at least five
     minutes apart;
   - `checks`: at least one, each id a built-in check and listed once, `params` an object
     whose keys are parameters of that check and whose values the core accepts.
2. **Next run.** `next_run_at` is the first firing of the schedule after now, in UTC. It is
   computed when the suite is created, when `schedule` or `enabled` changes, and after each
   firing. It is null without a schedule or while the suite is disabled.
3. **The scheduler.** A periodic task runs every minute. For each suite the claim function
   returns, in the suite's org:
   - when a run of the suite is still queued or running, no run is started (a slow suite does
     not pile up);
   - otherwise a run is created and queued in one transaction, `last_run_at` is set;
   - in both cases `next_run_at` becomes the first firing after now, so downtime is followed
     by one run, not by one per missed firing.
   A worker that dies between the claim and that update leaves the lease: the suite fires
   again ten minutes later.
4. **A suite's run** is a dataset run (spec 008) over the dataset's window at that moment,
   with `trigger = "suite"`, the suite's id and a copy of its checks. The executor passes the
   enabled checks with their parameters to the core, so only those run. A later change to
   the suite does not change a run that is already queued.
5. **Run now** starts the same run at once, whatever the schedule, also for a disabled suite.
   Without a dataset it is a 409 `no_dataset`.
6. **Deleted dataset.** The suite keeps its configuration, shows that the dataset is gone,
   and is skipped by the scheduler until it is given a dataset.
7. **Audit.** `suite.created`, `suite.updated` (with what changed), `suite.deleted` and
   `suite.run_started` (manual starts only) are written to the audit log.

## Acceptance criteria

- [x] The catalogue lists all 24 built-in checks with their default parameters, and every
      parameter with a null default has an `auto` entry.
- [x] Creating, reading, listing, changing and deleting suites works with the validation of
      behaviour 1, and another workspace's ids are 404.
- [x] A scheduled suite produces a run without a user: after the scheduler tick at a due
      time there is a queued run with `trigger = "suite"` and the suite's id, and the tick
      a minute later starts none.
- [x] That run executes only the suite's enabled checks with the suite's parameters.
- [x] A suite whose previous run has not finished is skipped and fires at its next time.
- [x] After downtime over several firings, one run is started.
- [x] `thresholds` names override, default, metadata and baseline correctly.
- [x] `GET /api/runs?suite_id=` is the suite's run history.
- [x] The web pages list, create, edit, run, disable and delete suites, and show each
      threshold's source.
- [x] Writes are refused for viewers (403) and recorded in the audit log.

## Test cases

- **Core** (`registry::tests`): `catalogue_covers_every_builtin_check`,
  `catalogue_marks_every_auto_parameter`, `validate_reports_unknown_checks_and_bad_params`.
- **Bindings**: `test_check_catalogue_and_validate_configs`.
- **API** (`api/tests/db/test_suites_api.py`): `crud_and_defaults`, `validation_422_409`,
  `thresholds_name_their_source`, `roles_and_audit`, `run_now_runs_only_the_suites_checks`,
  `run_now_without_dataset_409`, `history_by_suite_id`.
- **Scheduler** (`api/tests/db/test_suite_schedule.py`): `due_suite_gets_one_run`,
  `not_due_disabled_or_unscheduled_suites_do_not_fire`, `busy_suite_is_skipped`,
  `downtime_gives_one_run`, `lease_refires_after_a_lost_worker`,
  `next_run_follows_the_schedule` (pure).
- **Tenancy**: the suite routes join `ROUTES` in `tests/db/test_rls.py`; migration 0012
  upgrades and downgrades.
- **Web** (Vitest): `suites.list`, `suites.form` (presets, cron text, check switches and
  parameters, API errors by field), `suites.detail` (threshold sources, run now, history),
  `suites.schedule` (cron in words).

## Implementation notes

- The list was specified "by name"; it is newest first, on the `(created_at, id)` keyset every
  other list uses, and the web page sorts the suites by name.
- A parameter whose default is the text `auto` (for example `tby.flatline` `min_duration`)
  is shown as source `default` with the value `auto`: the core resolves it from the sampling
  interval inside the check, and the catalogue only names a source for null defaults.
- The form sends every catalogue check with `enabled` true or false and only the parameters
  that differ from the default, so a later change of a default reaches existing suites.
- A check the core no longer ships is left out of a stored suite's response and of its runs.
- The claim sets a ten-minute lease in `next_run_at` itself, so no extra column is needed and
  a second worker's tick in the same minute claims nothing.

## Out of scope

- Baselines that are stored and versioned, so a threshold shows its learned number: S10-2.
- Time zones for schedules and "on arrival of new data": with the alerting work, S10-5.
- Suites over a single series or an ad-hoc selection: a dataset is the unit (spec 008).
- A web page to create datasets: the form lists the datasets the API has.
- Per-series overrides inside a suite: later, with packs (S12).
