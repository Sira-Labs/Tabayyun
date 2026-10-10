# Spec 022 — PI Web API connector

Sprint 9, story S9-2. Depends on: 004 (series metadata), 021 (connector framework). Packages:
`api/` (`tabayyun.connectors`, fetch engine, sources API, jobs, migration 0009), `deploy/`,
`docs/`.

## Goal

A source of type `pi_web_api` reads an AVEVA PI System through PI Web API, the only
cross-platform route to it (`docs/research/04-backend-core-stack.md`). An admin creates the
source and stores its credentials. Then:

- **Search.** An editor searches the PI Data Archive's points by name, and the AF attributes of
  one AF database if one is configured. The search runs as a job in the worker and its matches
  land on the job's row.
- **Register.** The editor registers the chosen points as series, through the existing series
  route of spec 021.
- **Fetch.** Fetches, polls and dataset runs read the series' recorded values through the
  spec 021 engine. PI's quality flags and system digital states are mapped to Tabayyun's four
  qualities.
- **Import metadata.** A metadata job fills each series' unit, physical and operational limits
  and asset path from PI and AF. It keeps PI's exception and compression settings (`ExcDev`,
  `CompDev`, `CompMax`, …) in the series metadata for the compression-aware checks of S16-2.

The framework gains what this needs and every connector can use:

- search and metadata jobs, with a parameters and a result column on `source_fetches`;
- per-point failures that do not fail a whole fetch;
- a trusted certificate authority per connector;
- an injectable transport, so tests can run against a fake server.

## User story

As the workspace admin of a plant with a PI System, I point Tabayyun at our PI Web API once.
My engineers then find their tags by name, add them, and see their history and limits in
Tabayyun without exporting CSVs.

## Interface

### Connector interface additions (`tabayyun.connectors`)

```python
@dataclass(frozen=True)
class PointFailure:            # fetch() may yield it instead of a batch
    series_id: uuid.UUID
    message: str

@dataclass(frozen=True)
class PointMetadata:
    unit: str | None = None
    description: str | None = None
    physical_min: float | None = None
    physical_max: float | None = None
    operational_min: float | None = None
    operational_max: float | None = None
    asset_path: str | None = None
    extra: Mapping[str, Any] = {}      # kept under series.metadata[<source type>]

@dataclass(frozen=True)
class PointDescription:
    series_id: uuid.UUID
    metadata: PointMetadata | None
    error: str | None = None

class Connector:
    async def describe(self, points: Sequence[PointRef]) -> list[PointDescription]: ...  # default: NotSupported
```

- `fetch` may yield `PointFailure` for a point it cannot read, such as a point deleted in PI.
  - The engine records no coverage for that point in that call.
  - The point's message goes to the fetch's `result.point_errors`.
  - A fetch whose calls succeeded except for point failures ends `partial` and is not retried.
    Its `error` reads `N point(s) failed: <first message>`.
- `synthetic` implements `describe`: each point's unit, name and generator settings. A point its
  config does not have is now a `PointFailure`; spec 021 yielded nothing for it, which recorded
  coverage for a point that does not exist.
- `NetPolicy.http_client(base_url, *, verify=...)` takes an `ssl.SSLContext` for a custom
  certificate authority. Verification is never turned off.
- `NetPolicy(transport=...)` takes an inner httpx transport, for tests only. The address
  checks and the pinning still run in front of it.

### `pi_web_api` connector

**Config** (`ConnectorConfig` keys from spec 021, plus):

| Key | Default | Meaning |
|---|---|---|
| `base_url` | required | `https://…/piwebapi`; https only, no query or fragment, at most 500 characters |
| `data_server` | required | `\\SERVER`, the PI Data Archive whose points are searched and read |
| `asset_database` | none | `\\AFSERVER\Database`; when set, search also covers its element attributes |
| `ca_pem` | none | PEM certificate(s) of a private certificate authority, at most 64 KB; must load |
| `max_count` | 10000 | values per recorded-values request, 1000 to 150000 (PI's default `MaxReturnedItemsPerCall`) |
| `requests_per_second` | 20 | as in spec 021, here for each HTTP request the connector makes |

**Credentials**: `{"kind": "basic", "username", "password"}` or `{"kind": "bearer", "token"}`.
Kerberos is out of scope.

**External ids** are PI paths:

- a PI point: `\\SERVER\TAG`;
- an AF attribute: `\\AFSERVER\Database\Element\…|Attribute`; the `|` tells them apart.

**HTTP** (every request):

- `Accept: application/json` and `X-Requested-With: tabayyun`, which PI Web API's CSRF
  defence requires on POST.
- Basic credentials or the bearer token.
- Requests are paced at `requests_per_second` within each connector method.
- Item paths resolve to WebIds once per connector instance:
  - for a fetch call, with one `POST batch` of `GET points?path=` and `GET attributes?path=`
    sub-requests;
  - for metadata, with individual `GET`s.

**Errors**:

| Response | ConnectorError |
|---|---|
| 401, 403 | `AuthError` (`PI Web API refused the credentials`) |
| 404 or 410 for one item path or stream | `PointFailure` in a fetch; a per-series error in describe |
| 429, 500, 502, 503, 504, timeout, connection error | retryable |
| TLS verification failure | not retryable; the message names `ca_pem` |
| other 4xx, non-JSON answer | not retryable, with PI's first `Errors` message, at most 200 characters |

**`check`**: `GET dataservers?path=<data_server>`, then `GET assetdatabases?path=` when an
asset database is set.

- `IsConnected: false` is a retryable error, `data server not connected`.
- A 404 is `data server not found` or `asset database not found`.

**`search(query, limit)`**:

- **Filter:** a query without `*` or `?` becomes `*query*`.
- **Points:** `GET dataservers/{webId}/points?nameFilter=&maxCount=limit` →
  `RemotePoint(external_id=Path, name=Name, unit=EngineeringUnits, description=Descriptor)`.
- **AF attributes:** with an asset database, also
  `GET assetdatabases/{webId}/elementattributes?attributeNameFilter=&searchFullHierarchy=true&maxCount=`.
  Only attributes whose data reference is `PI Point` are kept, since only those have recorded
  values. The result is capped at `limit` in total, points first.

**`fetch(points, start_ns, end_ns)`**, one stream at a time:

- **Request:** `GET streams/{webId}/recorded?startTime=&endTime=&boundaryType=Inside&maxCount=`.
- **Times** are ISO 8601 UTC with 7 fractional digits. The start is rounded down and the end up
  to PI's 100 ns. Rows outside `[start_ns, end_ns)` are dropped.
- **Paging:** PI returns the earliest `maxCount` values and does not say it cut the answer
  short. A full page is followed by another request from the last timestamp. Values already
  read at that timestamp are skipped, since PI keeps several values at one timestamp.
  - A page made up of one timestamp only cannot advance. It is a non-retryable error that
    names the point.
- **Quality**, the first rule that matches:

  | PI value item | value | quality |
  |---|---|---|
  | `Errors` present, `Value` null, or a system digital state (`{"IsSystem": true}`) such as `I/O Timeout` or `Shutdown` | NaN | `bad` |
  | a string or other non-numeric value | NaN | `bad` |
  | `Good: false` | the number | `bad` |
  | `Questionable: true` | the number | `uncertain` |
  | `Substituted: true` | the number | `estimated` |
  | otherwise | the number; booleans as 1/0, digital states by their code | `good` |

**`describe(points)`**:

- **PI point:**
  - `GET points?path=` gives `EngineeringUnits`, `Descriptor`, `Zero`, `Span`, `Step` and
    `PointType`.
  - `GET points/{webId}/attributes` gives `excdev`, `excdevpercent`, `excmin`, `excmax`,
    `compdev`, `compdevpercent`, `compmin`, `compmax` and `compressing`.
  - The physical range is `[Zero, Zero + Span]` when `Span > 0`.
- **AF attribute:**
  - `GET attributes?path=` gives `DefaultUnitsNameAbbreviation`, `Description`, `Step` and
    `Links.Point`.
  - The limit traits come from `GET attributes/{webId}/attributes?traitCategory=Limit`, their
    values from `GET streams/{traitWebId}/value`:
    - `LimitMinimum` and `LimitMaximum` are the physical range;
    - `LimitLo` and `LimitHi` are the operational range;
    - `LimitLoLo`, `LimitHiHi` and `LimitTarget` go to `extra`.
  - The element path (before `|`) is the asset path.
  - `Links.Point` names the underlying PI point. The connector reads only the WebId from that
    link, never the URL itself. The point's attributes above are then added to `extra`.

### Database (migration 0009)

- `source_fetches.trigger` gains `search` and `metadata`.
- `source_fetches` gains two columns:
  - `params` (jsonb, null): the request, `{query, limit}` or `{overwrite}`;
  - `result` (jsonb, null): the outcome. A search stores its matches, a metadata import what it
    changed, and a fetch its point errors.
- The downgrade drops the columns and restores the old trigger check `NOT VALID`, as 0008 does,
  so existing `search` and `metadata` rows stay.

### API (spec 021 routes, plus)

| Route | Role | Body | Answer |
|---|---|---|---|
| `POST /api/sources/{id}/search` | editor | `{query: 1–200 chars, limit: 1–1000 = 100}` | 202 `{id}`; 409 `not_a_connector`, `not_supported` |
| `POST /api/sources/{id}/metadata` | editor | `{series_ids?: ≤1000, overwrite: false}` | 202 `{id}`; 409 `not_supported`; 422 `unknown_series` |
| `GET /api/sources/{id}/fetches/{fetch_id}` | viewer | none | the fetch as in the list, plus `params` and `result`; 404 when not the source's |

- Both POST routes use the `source.fetch` rate limit.
- `source.fetch_requested` is audited with the trigger and the parameters.
- **Result shapes:**
  - search: `{"items": [{external_id, name, unit, description}], "truncated": bool}`, where
    `truncated` means `limit` was reached;
  - metadata: `{"updated": n, "unchanged": n, "failed": n, "series": {"<id>": {"changed":
    [field…]} | {"error": "…"}}}`;
  - fetch: `{"point_errors": {"<series id>": "…"}}`, only when a point failed.

### Metadata rules

1. **Without `overwrite`**, an imported value fills a column only when it is empty. With
   `overwrite`, an imported value replaces it. A missing value never clears one.
2. **Columns:** `unit` (at most 32 characters, else skipped), `physical_min`, `physical_max`,
   `operational_min`, `operational_max` and `asset_path`.
3. **Validation:** the merged values are validated like a PATCH (spec 004). Limits that would
   fail are skipped, and the series' result says so; the other fields still apply.
4. **`series.metadata[<source type>]`** is replaced on every import, with `description`, the
   `extra` keys and `imported_at`.
5. **Audit:** one `source.metadata_imported` event per job, with the counts. The actor is the
   user who asked.

## Behaviour

1. **Search request.** `POST …/search` creates a `source_fetches` row with `trigger=search` and
   `params={query, limit}`, then defers the source job under the `source:<id>` lock.
   - A connector class that does not override `search` answers 409 `not_supported` at once.
2. **Search job.** The worker builds the connector, as for a check, and calls
   `search(query, limit)`.
   - On success the fetch ends `succeeded` with the items in `result`.
   - On a `ConnectorError` it ends `failed` with the message.
   - Health is updated as for a check.
3. **Metadata request.** `POST …/metadata` validates the series as `POST …/fetches` does, then
   creates a `metadata` row with `series_ids` and `params={overwrite}`.
   - Answers 409 `not_supported` when the connector does not override `describe`.
   - A disabled source still answers checks, searches and metadata imports: disabling stops
     fetches and polls only, as in spec 021.
4. **Metadata job.** The worker calls `describe` for the series, in chunks of `max_points`.
   - It applies the metadata rules in one transaction at the end.
   - If any series failed, it ends `partial`, not retryable, with `N series failed: …`.
5. **Fetch.** A `PointFailure` takes its point out of that call's coverage. The fetch ends
   `partial`, not retryable, unless something worse ended it first.
6. **Dataset runs.** A run that fetches inline lists such a partial fetch in
   `stats.fetch_errors`, as spec 021 does.
7. **PI paging** is the connector's own concern: one engine call may make several HTTP requests,
   each paced at `requests_per_second`.

## Acceptance criteria

- [x] `POST /api/sources` with `type=pi_web_api` validates the config: https-only `base_url`,
  the path shapes of `data_server` and `asset_database`, a `ca_pem` that loads, and `max_count`
  bounds.
- [x] Credentials accept `basic` and `bearer` and nothing else; they are sent as Basic or Bearer
  and never logged.
- [x] Every request carries `X-Requested-With`; TLS is verified, against `ca_pem` when set.
- [x] `check` succeeds against the fixture server, and fails for a 401 (not retryable), a 503
  (retryable) and a disconnected data server.
- [x] Search returns PI points and, with an asset database, PI Point AF attributes, capped at
  `limit`; a bare word is wrapped in `*`.
- [x] A fetch reads recorded values with paging past `max_count`, without duplicating values at
  a page boundary and keeping several values at one timestamp.
- [x] Quality follows the table: system states and errors are NaN and `bad`, questionable is
  `uncertain`, substituted is `estimated`.
- [x] A deleted point yields `PointFailure`: the other points' data and coverage are kept, and
  the fetch ends `partial` with `result.point_errors`.
- [x] The metadata import fills unit, limits and asset path. It does not overwrite edited
  values without `overwrite`, skips invalid limits, and stores the compression settings under
  `metadata.pi_web_api`.
- [x] `POST …/search`, `POST …/metadata` and `GET …/fetches/{id}` work as specified, with roles,
  rate limits and audit events; viewers cannot start them.
- [x] Integration test: search, register, fetch, a dataset run with findings, and a metadata
  import, against the recorded fixture server.
- [x] `deploy/caprover.md` documents a PI source: network allow-list, certificate authority,
  credentials and the PI Web API settings it relies on.

## Test cases

Unit (`api/tests`):

- `test_pi_web_api_config.py`:
  - config validation, including base URL, paths, `ca_pem` and bounds;
  - the credentials model.
- `test_pi_web_api_values.py`:
  - timestamp parsing with 0 to 7 fractional digits and offsets, and time formatting;
  - the quality and value mapping table;
  - paging with duplicate timestamps across a page boundary;
  - the error on a single-timestamp page.
- `test_pi_web_api_connector.py`, against `pi_fake.FakePiWebApi`, a `MockTransport` that
  serves the recorded responses in `tests/fixtures/pi_web_api/`:
  - check: success, 401, 503 and a disconnected server;
  - search: points and AF attributes, the wildcard rule, the cap at `limit`;
  - fetch: values, quality, paging, a 410 as `PointFailure`, a 429 as retryable;
  - describe: a PI point and an AF attribute with traits and its point;
  - every request has the CSRF header and auth, and the batch is a POST.
- `test_net_policy.py`: `verify` reaches the transport; an injected transport still gets pinned
  requests.
- `test_fetch_jobs.py`: a `PointFailure` ends the fetch `partial`, not retryable, with point
  errors.

Integration (`api/tests/db`):

- `test_pi_source.py`, end to end against the fake server:
  - create the source and set credentials;
  - search, register, fetch, then run a dataset with findings;
  - import metadata, then re-import without and with `overwrite`;
  - a fetch with a deleted point.
- `test_sources_api.py`:
  - the search, metadata and fetch-detail routes, with roles, `not_supported` and
    `unknown_series`;
  - audit events.
- `test_migrations.py`: 0009 upgrade and downgrade.

The fixtures are built from the response shapes in AVEVA's PI Web API reference, not recorded
from a live server: the project has no PI test server (`docs/roadmap/sprints.md`, owner input
for S9). When one is available, `tests/fixtures/pi_web_api/README.md` explains how to replace
them with real recordings.

## Implementation notes

- **One job for three tasks.** Checks, searches and metadata imports share the unretried
  `check_source` job (`fetches.execute_task`). Disabling a source stops fetches and polls only.
- **One value per timestamp in the cache.** The connector returns every value PI holds,
  including several at one timestamp. The Parquet cache keeps one per timestamp (spec 006), so
  a run counts 1440 rows for the fake's 1441-value day.
- **Point failures** end a fetch `partial`, not retryable, and turn the source's health
  `degraded`: a deleted tag is worth an admin's look, not a retry storm.
- **AF search matches attribute names only.** `elementattributes` takes an attribute-name and
  an element-name filter, and they combine with AND. Searching the element name too would need
  a second request per search.
- **Spec edits made during implementation:**
  - the 0009 downgrade keeps `search` and `metadata` rows behind a `NOT VALID` check, as 0008
    does, instead of deleting them;
  - `POST …/metadata` no longer answers `source_disabled`;
  - any full page holding a single timestamp is an error, since the next request would return
    the same page;
  - `synthetic` reports unconfigured points as `PointFailure`.

## Out of scope

- Kerberos (Negotiate) authentication: it needs a GSSAPI stack in the image. Revisit at the
  pilot install (S13).
- `streamsets/recorded` for many points per request. PI's reference does not say whether
  `maxCount` counts per stream there, so truncation could go unnoticed; revisit with a live
  server.
- Interpolated, plot and summary reads, and writing to PI.
- Compression-aware checks using the imported `ExcDev`/`CompDev`/`CompMax` (S16-2).
- The web pages for sources, search and import (S9-4).
- OPC UA (S9-3) and directory sources (S9-6).
