# Spec 021 — Connector framework

Sprint 9, story S9-1. Depends on: 004 (sources and series), 006 (Parquet cache and coverage),
007 (RLS, roles), 008 (dataset runs), 014 (audit log), 015 (rate limits). Packages: `api/`
(`tabayyun.connectors`, `tabayyun.secrets`, sources API, jobs, migration 0008), `deploy/`
(settings), `docs/`. Checklist items: `docs/frontend/02-security-baseline.md` (connector
credentials, SSRF).

## Goal

A source can be a connector that pulls observations from an external system into the Parquet
cache. The framework handles batching, rate limits, retries and health; every connector only
lists points and returns data for a window:

- **Connectors** implement a small Python interface. They are registered by source type and
  run only in the job worker, never in a request.
- **`fetch_window`** is the job that fills a source's coverage gaps:
  - it splits the window into calls the connector allows and paces them;
  - it writes each batch to the cache and records coverage;
  - it retries transient errors and updates the source's health.
- **Fetches happen three ways:** on demand through the API; on a poll interval per source; and
  inside a dataset run, for the parts of its window the cache lacks.
- **Credentials:**
  - they are write-only in the API;
  - they are encrypted at rest with an envelope key from the environment;
  - they are never logged or returned.
- **Network targets** are checked against an address policy before any connection (SSRF).
- **`synthetic`**, a built-in connector, generates deterministic series. It needs no credentials
  or network, so the framework can be tested end to end and staging has a live source for demos.

The PI Web API (S9-2), OPC UA (S9-3) and directory (S9-6) connectors build on this.

## User story

As the workspace admin of a plant install, I add a historian as a source once, with its
credentials. Its series then fill themselves and stay current. Dataset runs check what the
historian holds, not only what someone uploaded.

## Interface

### Connector interface (`tabayyun.connectors`)

```python
class Connector(ABC):
    type: ClassVar[str]                                  # = sources.type
    config_model: ClassVar[type[ConnectorConfig]]        # pydantic, extra="forbid"
    credentials_model: ClassVar[type[BaseModel] | None]  # None: takes no credentials

    def __init__(self, config: ConnectorConfig, credentials: BaseModel | None, net: NetPolicy): ...
    def limits(self) -> Limits: ...          # max_points per call, max_span_ns per call, requests_per_second
    async def check(self) -> None: ...       # reach the system and authenticate; raises ConnectorError
    async def search(self, query: str, limit: int) -> list[RemotePoint]: ...   # default: NotSupported
    def fetch(self, points: Sequence[PointRef], start_ns: int, end_ns: int) -> AsyncIterator[FetchedBatch]: ...
```

- **`FetchedBatch`** carries `series_id` and a `pyarrow.Table` with these columns:
  - `ts` (int64 ns UTC);
  - `value` (float64; NaN for null);
  - `quality` (utf8, one of `good`, `uncertain`, `bad`, `estimated`).

  A connector may return several batches per point and per call.
- **`ConnectorError(message, retryable: bool)`** is the only exception the framework expects.
  `AuthError` is a non-retryable subclass. Any other exception counts as non-retryable, is
  logged with its type only, and is reported as `internal connector error`.
- **`ConnectorConfig`** is the base config model. Every connector's config inherits its keys,
  with these defaults:

  | Key | Default | Meaning |
  |---|---|---|
  | `poll_interval_s` | none (no polling) | how often to poll; 60 or more |
  | `backfill_s` | 86400 | how far back a poll looks |
  | `settle_s` | 300 | data newer than now minus this is fetched but not counted as covered |
  | `requests_per_second` | 5 | at most 50 |
  | `max_points` | 100 | points per call, at most 1000 |
  | `max_span_s` | 604800 | span of one call, at most 31 days |

  A connector may lower these limits in `limits()`, but never raise them.
- **Registry:** `register(cls)` and `get(type)`. A source type without a connector, such as
  `upload`, is not a connector, and the connector routes answer 409 `not_a_connector` for it.

### `synthetic` connector

- **Config:** `interval_s` (default 60, minimum 1), `seed` (default 0), and `points`: a list of
  `{external_id, name?, unit?, base, amplitude, period_s, noise, faults}`.
  - At most 50 points; `faults` is a subset of `spikes` and `flatline`.
- **Values** are a pure function of `(seed, external_id, ts)`, so a re-fetch returns the same
  rows:
  - a sine wave plus hashed noise;
  - `spikes`: every 997th sample is 8 amplitudes high;
  - `flatline`: 02:00–03:00 UTC each day holds the 02:00 value;
  - quality is always `good`.
- `check` always succeeds. `search` lists the configured points.

### Network policy (`tabayyun.connectors.net`)

- `NetPolicy.resolve(host, port) -> list[IPAddress]` resolves once, then checks every address
  it gets:
  - **always refused:** loopback, link-local (cloud metadata), unspecified, multicast, broadcast,
    and IPv4-mapped forms of these;
  - **private ranges** (RFC 1918, ULA, CGNAT): allowed only inside
    `TABAYYUN_CONNECTOR_ALLOWED_NETWORKS`;
  - **public addresses:** allowed unless `TABAYYUN_CONNECTOR_ALLOW_PUBLIC=false`.
  - Any refused address raises `ConnectorError("target not allowed: <host>", retryable=False)`.
- `NetPolicy.http_client(base_url)` builds an `httpx.AsyncClient` for HTTP connectors:
  - redirects are off;
  - it connects to the address resolved above, sending the original Host header and TLS SNI,
    so a second DNS answer cannot redirect it;
  - timeout 30 s.

### Credentials (`tabayyun.secrets`)

- **Encryption:** AES-256-GCM.
  - The key is `TABAYYUN_MASTER_KEY`: base64 of 32 bytes.
  - The additional authenticated data is `"{org_id}:{source_id}"`, so a ciphertext copied to
    another source does not decrypt.
  - A key id (the first 8 hex of the key's SHA-256) is stored with each row.
- **Rotation:**
  - `TABAYYUN_MASTER_KEY_PREVIOUS` still decrypts old rows.
  - `python -m tabayyun.secrets rotate` re-encrypts every row under the current key and
    prints the number of rows.
- **Without a master key**, storing credentials answers 503 `credentials_unavailable`.
- **In prod**, a master key that is set but not 32 bytes of base64, or that is a known
  placeholder, refuses startup like the other secrets.

### Database (migration 0008)

- **`sources.type`** gains `synthetic`.
- **`sources`** gains:
  - `enabled` (boolean, default true);
  - `updated_at`;
  - `polled_at` (timestamptz, null), the time of the last poll.
- **`source_credentials`**, one row per source, under RLS by org:

  | Column | Type |
  |---|---|
  | `source_id` | uuid, primary key, references `sources` |
  | `org_id` | uuid |
  | `key_id` | text |
  | `nonce` | bytea |
  | `ciphertext` | bytea |
  | `updated_by` | uuid, null |
  | `updated_at` | timestamptz |

  The app login can read and write it, and only `tabayyun.secrets` reads it.
- **`source_fetches`**, the fetch history, under RLS by org (tenant columns):

  | Column | Type |
  |---|---|
  | `id` | uuid, primary key |
  | `org_id`, `workspace_id` | uuid |
  | `source_id` | uuid |
  | `trigger` | `manual`, `poll`, `run` or `check` |
  | `status` | `queued`, `running`, `succeeded`, `partial` or `failed` |
  | `window_start`, `window_end` | timestamptz |
  | `series_ids` | uuid[], null for all of the source's series |
  | `calls` | integer |
  | `rows` | bigint |
  | `error` | text |
  | `requested_by` | uuid, null |
  | `run_id` | uuid, null |
  | `created_at`, `started_at`, `finished_at` | timestamptz |

  It is indexed on (`source_id`, `created_at` desc). Rows older than 30 days are pruned by the
  maintenance job.

### Health (`sources.health`)

```json
{"status": "ok | degraded | failing | unknown", "checked_at": "...", "last_success_at": "...",
 "consecutive_failures": 0, "last_error": {"at": "...", "message": "...", "retryable": true},
 "last_fetch": {"id": "...", "status": "succeeded", "finished_at": "...", "rows": 1440}}
```

- `status` reads `degraded` after 1–2 consecutive failures and `failing` from 3.
- A success resets it to `ok`.
- A new source reads `unknown`.

### API (all under the workspace from `X-Tabayyun-Workspace`)

| Route | Role | Body or query | Answer |
|---|---|---|---|
| `GET /api/sources` | viewer | none | adds `enabled`, `health.status` and `connector` (bool) to each item |
| `POST /api/sources` | admin | `{type, name, config}` | 201 source; 422 `invalid_config` with the field errors; 409 `name_taken`; 422 `not_a_connector` for `upload` |
| `GET /api/sources/{id}` | viewer | none | `{id, type, name, enabled, config, health, credentials: {set, updated_at}, n_series, polled_at, created_at, updated_at}`; never a credential value |
| `PATCH /api/sources/{id}` | admin | `{name?, config?, enabled?}` | 200; `config` replaces the whole config and is validated |
| `PUT /api/sources/{id}/credentials` | admin | the connector's credentials model | 204; 422 if the connector takes none; 503 `credentials_unavailable` |
| `DELETE /api/sources/{id}/credentials` | admin | none | 204 |
| `POST /api/sources/{id}/check` | editor | none | 202 `{fetch_id}`; the result lands in `health` |
| `POST /api/sources/{id}/series` | editor | `[{external_id, name, unit?, kind?}]`, at most 500 | 200 `{created, existing}`; registers points as series of the source |
| `POST /api/sources/{id}/fetches` | editor | `{start, end, series_ids?, force?}` | 202 `{id}`; 409 `source_disabled`; 422 `invalid_window` (end at or before start, or more than 366 days) |
| `GET /api/sources/{id}/fetches?limit=` | viewer | none | the latest fetches, newest first, at most 100 |

- Changes write audit events:
  - `source.created`, `source.updated` and `source.fetch_requested`, with details naming the
    changed keys;
  - `source.credentials_set` and `source.credentials_cleared`, with no values.
- New rate-limit buckets:
  - `source.write`: 60 per minute per user, on the admin routes;
  - `source.fetch`: 30 per minute per user, on `check` and `fetches`.

### Jobs

| Task | Queue | Lock | Retry |
|---|---|---|---|
| `tabayyun.fetch_window` (`fetch_id`, `org_id`) | `fetch` | `source:<id>` | 5 attempts, exponential from 10 s, on retryable errors only |
| `tabayyun.check_source` (`fetch_id`, `org_id`) | `fetch` | `source:<id>` | none |
| `tabayyun.poll_sources` | `maintenance`, every minute | none | none |

The worker also listens on `fetch`. With `TABAYYUN_INLINE_JOBS=true`, the API runs fetches
after the response, as it does for runs.

### Settings

| Setting | Default |
|---|---|
| `TABAYYUN_MASTER_KEY` | none |
| `TABAYYUN_MASTER_KEY_PREVIOUS` | none |
| `TABAYYUN_CONNECTOR_ALLOWED_NETWORKS` | empty (comma-separated CIDRs) |
| `TABAYYUN_CONNECTOR_ALLOW_PUBLIC` | `true` |
| `TABAYYUN_RUN_FETCH_BUDGET_S` | `120`; `0` turns fetching inside runs off |

## Behaviour

1. **Creating a source** validates `config` against the connector's model:
   - unknown keys and out-of-range limits answer 422 with the errors per field;
   - the source starts `enabled` with `health.status = unknown`;
   - an audit event is written.
2. **Credentials:**
   - `PUT` validates the body against the connector's credentials model, encrypts it and upserts
     the row.
   - Responses and logs never contain the values, and neither does the audit event. Errors from
     the connector name the source, never a credential.
3. **A fetch request** stores a `queued` `source_fetches` row and defers `fetch_window` in the
   same transaction.
4. **`fetch_window`:**
   1. It loads the source in the fetch's org context. It decrypts the credentials, builds the
      connector, and marks the fetch `running`.
   2. For each series (the requested ones, else all of the source's series), it computes the
      gaps between the fetch window and the coverage. `force` ignores coverage.
   3. It cuts the gaps into calls: spans of at most `max_span`; series with the same span are
      grouped up to `max_points`. Calls run in time order, paced to `requests_per_second` by a
      token bucket per source (one fetch per source at a time, through the job lock).
   4. Each call's batches are written to the raw layer of the cache, under the source's id.
      Coverage is recorded per series for the call's span, but only up to `now - settle_s`:
      - a span the system answered with no rows still counts as covered;
      - newer data is fetched again next time.
   5. **Errors:**
      - A retryable `ConnectorError` stops the fetch; progress so far stays covered. The fetch
        is marked `partial` with the error and retried by Procrastinate; the retry fetches only
        what is still missing.
      - A non-retryable error marks it `failed`, with no retry.
   6. **Afterwards**, the fetch records its calls and rows and the health is updated:
      - the outcome is `succeeded` when every call ran, `partial` or `failed` otherwise;
      - the final attempt's state is the fetch's state.
5. **`check_source`** calls `check()` and updates the health only. The fetch row records the
   outcome with zero calls.
6. **`poll_sources`**, every minute, looks at enabled connector sources with a
   `poll_interval_s`:
   - for each source whose `polled_at` is older than its interval, it sets `polled_at` and
     defers a `poll` fetch over `[now - backfill_s, now)`;
   - the coverage gaps limit that fetch to what is new;
   - a source with a fetch still queued or running is skipped.
7. **Dataset runs** fill coverage gaps before reading:
   - for series of connector sources with gaps in the run window, the run calls the same fetch
     code inline, with `trigger = run` and the run's id;
   - all of a run's fetches together are limited to `TABAYYUN_RUN_FETCH_BUDGET_S` of wall time;
   - errors and a spent budget go to `stats.fetch_errors` and do not fail the run;
   - the run then re-reads coverage, so `stats.missing` shows what is still absent.
8. **A disabled source** is not polled, and fetch requests for it answer 409. Its data and
   series stay.
9. **Authorization:** every route works through `authorize`, as in spec 007. Another
   workspace's source answers 404, and an `upload` source answers 409 `not_a_connector` on
   connector routes.
10. **Logs:**
    - `fetch.started` and `fetch.finished`, with source, fetch, calls, rows and duration;
    - `fetch.failed`, with the error message and whether it is retryable;
    - `source.health_changed` when the status changes.

## Acceptance criteria

- [ ] Migration 0008 upgrades and downgrades. The new tables have RLS, and the app login can
      read neither another org's credentials nor its fetches.
- [ ] A `synthetic` source can be created, given points, and fetched over a day. The cache then
      holds 1440 rows per point at 60 s, coverage spans the day, and a second fetch makes no
      calls.
- [ ] The fetch engine:
  - [ ] splits a 30-day window over 250 points with `max_points=100` and `max_span_s=7d`
        into 15 calls;
  - [ ] keeps the call rate under `requests_per_second`;
  - [ ] does not mark the last `settle_s` as covered.
- [ ] Error handling:
  - [ ] a retryable error after 2 of 4 calls leaves those 2 covered, and the retry makes only the
        other 2;
  - [ ] a non-retryable error fails the fetch without a retry;
  - [ ] health moves ok → degraded → failing → ok.
- [ ] Credentials:
  - [ ] they round-trip encrypted, and the database holds no plaintext;
  - [ ] another source's AAD fails to decrypt;
  - [ ] `rotate` re-encrypts rows from the previous key;
  - [ ] no response, log line or audit event contains a credential value;
  - [ ] storing them without a master key answers 503.
- [ ] The network policy refuses loopback, link-local and metadata addresses always, private
      ranges outside the allow-list, and public ones when they are turned off. The HTTP
      client does not follow redirects and connects to the checked address.
- [ ] Polling defers one fetch per due source and skips sources with a fetch in flight.
- [ ] A dataset run over a synthetic source with an empty cache fetches inline and checks the
      data. A failing connector shows in `stats.fetch_errors`, and the run still succeeds on
      what is cached.
- [ ] Every route answers 404 across workspaces, and 403 for a role below the one in the table.
      Every change writes its audit event.
- [ ] The docs are updated: `deploy/caprover.md` settings and troubleshooting, the architecture
      data flow, and the baseline items for connector credentials and SSRF.

## Test cases

**Unit (`api/tests`):**
- `test_connector_plan.py`: call planning (spans, point groups, ordering), settle cut-off,
  token-bucket pacing with a fake clock.
- `test_synthetic_connector.py`: determinism across fetches and windows, the `spikes` and
  `flatline` faults, and config validation.
- `test_net_policy.py`: every refused range (v4, v6, IPv4-mapped), the allow-list, public off,
  a host resolving to one good and one bad address (refused), no redirects, and the pinned
  address with Host and SNI.
- `test_secrets.py`: round trip, wrong AAD, wrong key, rotation, prod refusal of a bad key.

**Integration (`api/tests/db`):**
- `test_sources_api.py`: create, validate, patch, credentials set and clear (values never
  echoed), registering series, roles, 404 across workspaces, audit events, rate limits.
- `test_fetch_window.py`:
  - fetching with the synthetic connector into a local cache;
  - idempotence, a partial fetch with retry, a non-retryable failure;
  - health transitions, the job lock, inline jobs.
- `test_poll_sources.py`: due and not-due sources, in-flight skip, disabled source.
- `test_dataset_run_fetch.py`: an inline fetch inside a run, the budget, errors in
  `stats.fetch_errors`.
- `test_migrations.py`: the new tables and RLS on them.

## Decisions

1. **Fetch inside dataset runs, not as separate jobs.** The architecture text had the worker
   enqueue `fetch_window` jobs for a run's gaps and wait. Fetching inline uses the same code,
   keeps the run's view of the data consistent, and needs no job-to-job waiting. Its cost is
   bounded by a budget. Scheduled suites (S10-1) can still fetch ahead through polling.
2. **Credentials in Postgres under an envelope key**, as the security baseline asks:
   - write-only;
   - bound to their source by the AAD;
   - rotatable.

   There are no `env:` or `file:` references, which would let an admin point a connector at
   any of the server's own secrets.
3. **Private networks are opt-in.** Historians are usually on private networks, so on-premise
   installs set `TABAYYUN_CONNECTOR_ALLOWED_NETWORKS`. The default keeps a hosted install from
   reaching its own internal services.
4. **Coverage ends at `now - settle_s`.** Historians accept late and back-filled data for
   minutes after the fact. Without settling, a poll would mark the newest minutes covered and
   never re-read them.
5. **A `synthetic` connector ships**, not just a test double. It is the framework's end-to-end
   fixture, and it gives staging a live, polling source for demos without a historian.

## Out of scope

- PI Web API (S9-2), OPC UA (S9-3) and directory sources (S9-6), and point search or browse
  jobs, which come with S9-2.
- The web pages for sources and health (S9-4).
- Deleting a source and its cached data, which needs retention (S13).
- Scheduled suites that run checks after a poll (S10-1).
- Writing back to a historian (S18).
