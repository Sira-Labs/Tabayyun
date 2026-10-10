# Spec 023 — OPC UA connector

Sprint 9, story S9-3. Depends on: 021 (connector framework), 022 (search and metadata jobs,
point failures). Packages: `api/` (`tabayyun.connectors.opc_ua`, network policy, sources API),
`deploy/`, `docs/`. Decision: ADR-0018 (asyncua, LGPL-3.0).

## Goal

A source of type `opc_ua` reads an OPC UA server's historical data with `asyncua`, through the
spec 021/022 jobs:

- **Connect.** Signed, or signed and encrypted, with the client certificate Tabayyun generates
  for the source. The server certificate is pinned by its SHA-256 thumbprint. The user logs on
  as anonymous, or with a username and password. Unencrypted connections need an explicit
  opt-in.
- **Search** walks the address space and matches variables by name.
- **Fetch** reads raw history for many nodes in one request, follows the server's continuation
  points, and maps OPC UA status codes to Tabayyun's four qualities. `Good_LocalOverride` and
  `Good_Clamped` become `uncertain`.
- **Metadata** comes from the standard properties: unit from `EngineeringUnits`, the physical
  range from `InstrumentRange`, the operational range from `EURange`, and the asset path from
  the browse path.

## User story

As the workspace admin of a plant whose SCADA or historian exposes OPC UA, I add the server
once and confirm its certificate. I give the server Tabayyun's client certificate. My engineers
then find variables by name and get their history checked like any other series.

## Interface

### Network policy

- `NetPolicy.tcp_target(host, port) -> str` resolves and checks the host as spec 021 does
  (`TargetRefusedError` otherwise), and returns the address to connect to.
- `NetPolicy(redirect={address: address})` maps a checked address to another one. It is for
  tests only, so they can reach a server on loopback while the checks still run.

### Connector additions (`tabayyun.connectors.Connector`)

- `prepare_credentials(new, previous, *, source_id) -> BaseModel`, a class method. It lets a
  connector complete credentials before they are stored. The default returns `new`.
- `client_certificate(credentials) -> dict | None`, a class method. It returns the public part
  of a client identity, if the connector has one. The default returns None.

### `opc_ua` connector

**Config** (`ConnectorConfig` keys from spec 021, plus):

| Key | Default | Meaning |
|---|---|---|
| `endpoint_url` | required | `opc.tcp://host:port[/path]`, at most 500 characters |
| `security_policy` | `Basic256Sha256` | `Basic256Sha256`, `Aes128_Sha256_RsaOaep`, `Aes256_Sha256_RsaPss`, or `None` |
| `security_mode` | `SignAndEncrypt` | `Sign` or `SignAndEncrypt`; ignored for policy `None` |
| `allow_insecure` | `false` | must be `true` for policy `None` |
| `server_certificate_sha256` | none | the pinned thumbprint: 64 hex digits, colons allowed; required to connect unless the policy is `None` |
| `browse_root` | `i=85` | the node a search starts from (the Objects folder) |
| `browse_limit` | 5000 | nodes a search visits at most, 100 to 50000 |
| `browse_depth` | 10 | levels a search goes down at most, 1 to 30 |
| `max_values` | 10000 | values per node per history request, 100 to 100000 |
| `timeout_s` | 30 | request timeout, 1 to 300 |
| `requests_per_second` | 20 | as in spec 021, for each OPC UA request |

**Credentials:**

```json
{"kind": "anonymous" | "username", "username": "...", "password": "...",
 "client_certificate_pem": "...", "client_private_key_pem": "..."}
```

- The client certificate and key are optional, and come both or neither.
- **Without them,** `prepare_credentials` keeps the source's earlier pair. If there is none, it
  generates one:
  - RSA 2048 with SHA-256, valid 5 years;
  - subject `CN=Tabayyun <source id>`;
  - subject alternative name `URI:urn:tabayyun:source:<source id>`;
  - key usage for an OPC UA client, extended key usage `clientAuth`.
- **A provided pair** must be RSA of at least 2048 bits, and the key must match the
  certificate.
- The application URI the client presents is the certificate's URI.

**External ids** are node ids as OPC UA writes them: `ns=2;s=FIC101.PV`, `ns=3;i=1001`,
`i=2258`, and so on.

**Connecting** (`check`, `search`, `fetch`, `describe`):

1. Parse the endpoint, then `tcp_target(host, port)`. The client connects to the checked
   address, keeping the endpoint's port and path.
2. **For a policy other than `None`:**
   - Get the endpoints over an unsecured discovery channel, which OPC UA allows for
     `GetEndpoints`.
   - Pick the endpoint with the configured policy and mode. The server not offering it is a
     non-retryable error.
   - Take the endpoint's server certificate and compute its SHA-256 thumbprint.
     - Unpinned: a non-retryable error,
       `server certificate not pinned: SHA-256 <thumbprint>, subject <CN>`.
     - A different thumbprint: a non-retryable error naming both thumbprints.
   - Secure the channel with the client certificate. Without one: `AuthError`, telling the
     admin to set credentials first.
3. **For policy `None`:** only with `allow_insecure`, and with no certificate pinning.
4. **The user identity:** anonymous, or the username and password.

**Errors:**

| Condition | ConnectorError |
|---|---|
| connection refused, timeout, `BadTimeout`, `BadServerHalted`, `BadTooManySessions`, `BadCommunicationError`, `BadConnectionClosed`, `BadSecureChannelClosed` | retryable |
| `BadUserAccessDenied`, `BadIdentityTokenInvalid`, `BadIdentityTokenRejected`, `BadCertificate*`, `BadSecurityChecksFailed`, `BadSecurityPolicyRejected` | `AuthError` (`OPC UA server refused the connection: <code name>`) |
| any other `Bad*` status on the session | not retryable, with the code name |

**`check`** connects, reads the server's `ServerStatus.State`, and fails retryably unless the
state is `Running`.

**`search(query, limit)`:**

- Walks the hierarchy breadth-first from `browse_root`, along hierarchical references except
  `HasProperty`.
- Sends 100 nodes per Browse request and follows `BrowseNext`.
- Stops at `browse_limit` nodes or `browse_depth` levels. A variable is never entered twice.
- **Matching:** a query without `*` or `?` matches when it appears in the display name or browse
  name, ignoring case. Otherwise it is matched as a wildcard pattern.
- **Results:** `RemotePoint(external_id=<node id>, name=<display name>, description=<browse
  path>)`, at most `limit`.

**`fetch(points, start_ns, end_ns)`:**

1. **Check the nodes.** One Read request gets `NodeClass` and `AccessLevel` of every node.
   - A node id that does not parse, a Bad status, a non-variable, or an `AccessLevel` without
     the HistoryRead bit is a `PointFailure` naming the reason.
2. **Read history.** One HistoryRead request with `ReadRawModifiedDetails` covers all valid
   nodes:
   - `StartTime` and `EndTime` are the window, rounded to microseconds, which is asyncua's
     resolution;
   - `NumValuesPerNode=max_values`, `ReturnBounds=false`, source timestamps.
   - Nodes with a continuation point are read again until none remains.
   - On an error, the continuation points are released (`ReleaseContinuationPoints`).
   - A node's Bad result (other than `BadNoData`) is a `PointFailure` with the code name.
3. **Rows** take `SourceTimestamp`, else `ServerTimestamp`, inside `[start_ns, end_ns)`.
   - **Values:** numbers and booleans as float. Anything else, or a non-finite number, is NaN
     with quality `bad`.
   - **Quality** is decided by the status code without its info bits, first match wins:

     | Status | Quality | Value |
     |---|---|---|
     | Bad severity | `bad` | NaN |
     | `Good_LocalOverride`, `Good_Clamped` | `uncertain` | kept |
     | other Good | `good` | kept |
     | `Uncertain_SubstituteValue` | `estimated` | kept |
     | other Uncertain | `uncertain` | kept |

**`describe(points)`, per node:**

- It reads `DisplayName`, `Description`, `DataType` and `AccessLevel`, and the properties
  `EngineeringUnits`, `EURange` and `InstrumentRange`.
- It walks the parents up to `Objects`, at most 20 levels.
- **Mapping:**
  - unit: the `EngineeringUnits` display name;
  - physical range: `InstrumentRange`;
  - operational range: `EURange`;
  - asset path: the parent's browse path, joined with `/`;
  - description: the node's description;
  - `extra`: `node_id`, `data_type`, the unit's id and namespace, both ranges as read, and
    `history_read` (whether `AccessLevel` allows it).
- A node that cannot be read gets a per-series error, as in spec 022.

### API

| Route | Role | Answer |
|---|---|---|
| `GET /api/sources/{id}/client-certificate` | viewer | 200 `{certificate_pem, sha1, sha256, application_uri, not_after}`; 404 `no_client_certificate` |

- `PUT /api/sources/{id}/credentials` for `opc_ua` keeps or generates the client identity
  before encrypting.
- Its audit event `source.credentials_set` gains `{"client_certificate": "generated" | "kept" |
  "provided"}`.
- The private key is never returned.

## Behaviour

1. **Set up.** An admin creates an `opc_ua` source with `endpoint_url`, then sets credentials,
   for example `{"kind": "anonymous"}`. Tabayyun generates the client certificate.
2. **Trust.** The admin downloads the certificate from `client-certificate` and trusts it on
   the server.
3. **Pin.** The first check fails, naming the server certificate's thumbprint. The admin
   compares it with the server's, then sets `server_certificate_sha256` with `PATCH`. The next
   check succeeds.
4. **Use.** Search, register, fetch, poll, runs and metadata import work as in spec 022.

## Acceptance criteria

- [x] Config validation: `opc.tcp` URLs only, policy and mode values, `None` only with
  `allow_insecure`, thumbprint format, and bounds.
- [x] Credentials: anonymous or username. A client certificate is generated, kept across
  credential updates, or accepted when provided and valid; a mismatched pair is refused.
- [x] `GET …/client-certificate` returns the public certificate and thumbprints, never the
  key, and 404 without one.
- [x] The first check without a pin fails with the server's thumbprint. With the right pin it
  succeeds; with a wrong pin it fails, not retryable.
- [x] Connections go only to addresses the network policy allows. The test redirect is the
  only way to reach loopback.
- [x] Search finds variables by name below `browse_root`, skips properties, and keeps to
  `browse_limit` and `limit`.
- [x] A fetch reads history for several nodes with continuation points. Quality follows the
  table, including `Good_LocalOverride` and `Good_Clamped` as `uncertain`.
- [x] Unknown nodes, non-variables and nodes without history access are `PointFailure`s; the
  other nodes' data is kept.
- [x] `describe` fills unit, `InstrumentRange`, `EURange` and asset path.
- [x] Integration test against an `asyncua` server fixture: set up, pin, search, register,
  fetch, a dataset run with findings, and a metadata import.
- [x] `deploy/caprover.md` documents an OPC UA source; ADR-0018 records the licence decision.

## Test cases

Unit (`api/tests`):

- `test_opc_ua_config.py`: config validation; credentials; certificate generation, keeping
  and validation.
- `test_opc_ua_status.py`: the quality table over status codes with and without info bits;
  value conversion.
- `test_opc_ua_connector.py`, against `opcua_server.running_server()`, an in-process asyncua
  server on loopback reached through the test redirect:
  - check, with pinning (unpinned, pinned, wrong pin), a username login, a refused login and a
    connection refused;
  - search;
  - fetch, with continuation, quality and point failures;
  - describe.
- `test_net_policy.py`: `tcp_target` checks, and the redirect.

Integration (`api/tests/db`):

- `test_opc_ua_source.py`: end to end through the API, as in the behaviour above.
- `test_rls.py`: the new route.

## Implementation notes

- **Checking nodes first.** asyncua's test server answers a history read of an unknown or
  non-historized node with Good and no values. The fetch therefore reads `NodeClass` and
  `AccessLevel` of all its nodes first, in one Read. This also gives clear point failures on
  real servers.
- **Continuation points.** asyncua's test server ignores `NumValuesPerNode` for continuation:
  it cuts the answer without a continuation point. The tests page with the server's own page
  size; real servers follow the standard.
- **The client identity** is generated in a thread (`asyncio.to_thread`), so a 2048-bit key
  does not block the event loop of the api.
- **Spec edits made during implementation:** none.

## Out of scope

- Subscriptions and live values: polling reads history.
- Events and alarms history, aggregates (`ReadProcessedDetails`), `ReadAtTime`.
- Writing to OPC UA (S18-2).
- Quality-code sub-findings (S16-4).
- The web pages (S9-4).
- Trusting a server through a certificate authority instead of a pin. It can be added beside
  the pin when a site needs it.
- `nsu=` node ids, which survive a change in the order of the server's namespace array.
- Reverse connect, and servers behind an OPC UA gateway that rewrites endpoints.
