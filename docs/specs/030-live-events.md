# Spec 030 — Live events: an SSE stream for finished runs and new findings

Sprint 10, story S10-4. Depends on:
- spec 002: runs and the job queue; spec 008: dataset runs; spec 027: suites that start runs
  without a user;
- spec 007: tenancy; spec 013: the session; spec 014: workspaces and roles;
- spec 029: the findings inbox, the first page that updates live.

Packages: `api/src/tabayyun/` (events service, listener, route, run completion, migration
0016), `web/src/` (live hook in the page frame), `docs/`.

## Goal

A signed-in browser holds one stream to the API. When a run of its workspace finishes, or a
run stored new findings, the API says so on the stream within a second, whichever process
finished the run, and the pages showing runs, findings, series and suites reload what they
show. Nobody presses reload to see what a scheduled suite found.

## User story

As a data steward, I keep the findings inbox open. The suite of 02:00 runs on the worker; its
new findings appear in my list while I look at it, and the run I started myself turns to
"succeeded" on the runs page without the page asking every two seconds.

## Interface

### Events

An event is a JSON object. Inside the database it travels on the Postgres notification
channel `tabayyun_events`:

```json
{"v": 1, "type": "run.completed", "org_id": "…", "workspace_id": "…",
 "at": "2026-11-19T22:41:07.123456+00:00",
 "data": {"run_id": "…", "status": "succeeded", "n_findings_new": 3}}
```

| Type | Sent when | `data` |
|---|---|---|
| `run.completed` | a run turned `succeeded` or `failed` (also when the reaper fails a lost run) | `run_id`, `status`, `n_findings_new` |
| `finding.created` | a run that succeeded stored at least one new finding | `run_id`, `count` |

`finding.created` is one event per run with the number of new findings, not one per finding:
a dataset run can store thousands.

### API

`GET /api/events?workspace=<id>` (viewer) → `200 text/event-stream`:

```
retry: 5000

: connected

event: run.completed
data: {"type":"run.completed","at":"…","run_id":"…","status":"succeeded","n_findings_new":3}

event: finding.created
data: {"type":"finding.created","at":"…","run_id":"…","count":3}

event: resync
data: {"type":"resync"}

: keep-alive
```

- `workspace` names the workspace, because `EventSource` cannot send the
  `X-Tabayyun-Workspace` header; without it the header and then the default apply, as on
  every other route. 400 `invalid_workspace` when malformed.
- 401 without a session, 404 without a role in the workspace.
- 503 `too_many_streams` with `Retry-After: 30` when the process already serves
  `TABAYYUN_EVENTS_MAX_STREAMS` streams.
- `resync` says that events may have been missed (the stream's buffer overflowed, or the
  API lost its database connection): the client reloads what it shows.
- Headers: `Cache-Control: no-cache`, `X-Accel-Buffering: no`.

### Settings

| Key | Default | Meaning |
|---|---|---|
| `TABAYYUN_EVENTS_MAX_STREAMS` | 200 | streams one API process serves at a time |
| `TABAYYUN_EVENTS_HEARTBEAT_S` | 15 | seconds of silence before a `: keep-alive` comment |
| `TABAYYUN_EVENTS_MAX_AGE_S` | 900 | seconds after which the API ends a stream; the browser connects again, which checks the session and the role again |

### Database (migration 0016)

`tabayyun_reap_stale_runs(interval)` is replaced by a version that also notifies
`run.completed` (status `failed`) for every run it fails. No table changes.

### Web

`useLiveEvents()` in the page frame opens the stream for the current workspace and keeps it
for as long as the frame is mounted. On `run.completed` it reloads the queries of runs,
series, scores and suites; on `finding.created` those of findings (the inbox included); on
`resync`, and when the stream opens again after an error, all of them. Reloads are gathered:
at most one per second.

## Behaviour

1. **Publishing.** The transaction that completes a run calls `pg_notify` with the event.
   Postgres delivers a notification when its transaction commits and drops it on rollback,
   so an event never announces a run that was not stored, and needs no table.
2. **Listening.** Each API process holds one extra database connection that listens on the
   channel, opened with the first stream and kept until shutdown. It hands every event to
   the streams of that event's org and workspace. A stream holds no database connection:
   the role is checked in a short transaction before the stream starts.
3. **Losing the connection.** The listener connects again with a growing pause (1 s to
   30 s). After it is back, every stream gets `resync`, because notifications sent in
   between are gone.
4. **Slow clients.** A stream buffers up to 100 events. When the buffer is full it is
   emptied and the stream gets one `resync` instead.
5. **Ending.** A stream ends when the client goes away, when it is `MAX_AGE_S` old, or at
   shutdown. In each case its place is given back.
6. **Tenancy.** A stream only ever gets events whose `org_id` and `workspace_id` equal its
   own; events of other orgs and workspaces are dropped by the process before any stream
   sees them. The channel carries ids, a status and counts, and no data of a series.
7. **Malformed notifications** (not JSON, unknown version, missing ids) are logged and
   dropped.
8. **Without the stream** (blocked by a proxy, an old browser) the web app works as before:
   the run page keeps polling while a run is queued or running.

## Acceptance criteria

- [x] A run that succeeds publishes `run.completed` with its status and the number of new
      findings, and `finding.created` when that number is above zero; a failed run
      publishes `run.completed` with `failed`; a run that is rolled back publishes nothing.
- [x] The reaper's failed runs publish `run.completed`.
- [x] `GET /api/events` answers 401 without a session, 404 for a workspace of another org or
      one without a role, 400 for a malformed workspace, and 503 `too_many_streams` over the
      limit.
- [x] A stream gets the events of its workspace and none of another workspace or org.
- [x] A full buffer gives one `resync`; a listener that reconnected gives `resync` to every
      stream.
- [x] A silent stream sends `: keep-alive`; a stream older than the maximum age ends, and
      its place is free again.
- [x] The stream holds no database connection while it is open.
- [x] In the web app, the findings inbox shows a new finding after `finding.created` without
      a reload by the user, and the runs list a finished run after `run.completed`.
- [x] Several events within a second cause one reload; a reopened stream reloads everything.

## Test cases

- **API, no database** (`api/tests/test_events.py`): `encode_decode_round_trip`,
  `decode_refuses_malformed`, `broker_delivers_by_org_and_workspace`,
  `broker_overflow_resyncs`, `broker_limit_and_release`, `stream_formats_events`,
  `stream_keep_alive_and_max_age`, `sse_frames`.
- **API, database** (`api/tests/db/test_events.py`): `run_publishes_completed_and_created`,
  `failed_run_publishes_completed`, `rolled_back_run_publishes_nothing`,
  `reaper_publishes_completed`, `listener_delivers_and_resyncs_after_reconnect`,
  `route_refusals`, `route_streams_the_workspaces_events`.
- **Tenancy**: migration 0016 upgrades and downgrades; the reaper function stays callable
  by the app role only.
- **Web** (Vitest): `live.events` (opens the stream for the workspace, reloads the inbox
  after `finding.created` and the runs after `run.completed`, gathers reloads, reloads all
  after a reopen, closes on unmount, does nothing without `EventSource`).

## Implementation notes

- `finding.created` carries a count per run: one notification per finding would put
  thousands on the channel for one dataset run, and the page reloads its list either way.
- No replay (`Last-Event-ID`): a client that was away reloads what it shows, which is what
  it does on `resync` too. A table of past events would only serve this.
- The stream's maximum age is how a revoked session or role stops receiving events: there
  is no per-event check of the session.
- The listener opens with the first stream, so processes that never serve one (the worker,
  the CLI, most tests) hold no extra connection.
- The reverse proxy needs no change: Caddy flushes `text/event-stream` responses as they
  come, and nginx (CapRover) stops buffering on the response's `X-Accel-Buffering: no`.
- A stream the server refuses (401, 404, 503) is not retried by the browser, so the web app
  opens a new one after 30 seconds.
- The failed-run test fails a queued run through the service: an upload that cannot be
  parsed is refused with 422 before a run exists.

## Out of scope

- Events for changes people make (a finding acknowledged by a colleague, a suite edited):
  with alerts and the overview (S10-5, S10-6) when their pages need them.
- Showing the events themselves (a toast, a bell): S10-5 decides what deserves attention.
- Webhooks and email on the same events: S10-5.
- Replay of missed events and a durable event log.
