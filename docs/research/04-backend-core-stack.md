# Research 04 — Backend, compute core, storage, connectors and plugins

*Research date: 2026-09-21. Versions verified against crates.io / PyPI on that date unless
noted; items marked "unverified" could not be confirmed. Decisions derived from this are in
`docs/architecture/` and the ADRs.*

## 1. Recommended architecture (summary)

**"Rust in-process core, Python shell, Postgres-only infrastructure."**

```
┌─────────────────────────────────────────────────────────────────┐
│ Python (FastAPI 0.141 + Pydantic 2.13 + SQLAlchemy 2.0 async)   │
│  API · auth · orchestration · connector framework · plugin host │
│      │ zero-copy Arrow (PyCapsule) via PyO3 0.29 / maturin 1.15 │
│ Rust core wheel: `tabayyun_core` (Polars 0.55 lazy/streaming +  │
│   arrow 60 + augurs 0.10 + statrs 0.19 + changepoint 0.15)      │
├─────────────────────────────────────────────────────────────────┤
│ Jobs: Procrastinate 3.9 (Postgres LISTEN/NOTIFY, no broker)     │
│ Metadata + results/metrics: PostgreSQL 16/17 + TimescaleDB 2.30 │
│ Raw TS cache: Parquet on local disk/MinIO (object_store 0.14)   │
│   queried by Polars/DataFusion 55 (and DuckDB 1.5 for ad hoc)   │
│ Plugins: Python checks in a subprocess worker pool (cgroups),   │
│   optional WASM (wasmtime 48) lane for "untrusted" tier         │
│ Deploy: docker compose bundle (air-gapped tarball) + Helm chart │
│ Observability: OpenTelemetry (Rust 0.33 / Python SDK 1.44)      │
│   + Prometheus scrape endpoint                                  │
└─────────────────────────────────────────────────────────────────┘
```

**Rationale.** A data-quality product is batch/window-oriented, not a streaming database:
per-tag work is "load window → compute statistics → write small result rows." That favours
(a) an embedded Rust library rather than a network service (no serialization, one process to
ship), (b) Parquet-on-disk as the cache (cheap, portable, air-gap friendly, readable by every
engine), and (c) a single Postgres for everything transactional (metadata, results, job
queue), which is the easiest thing to install and back up behind a firewall.

**Main alternatives considered.** Rust as a separate Arrow Flight service (better process
isolation and horizontal scaling; pay with an extra binary, TLS/gRPC plumbing and an IPC
boundary; reasonable phase 2 when one node isn't enough). ClickHouse or QuestDB for raw TS
(faster ad-hoc analytics, but a second stateful system). Temporal/Hatchet for orchestration
(durable execution is attractive, but both add a server that must also be air-gapped).

## 2. Rust columnar/compute layer

| Option | Version (crates.io) | Strengths | Weaknesses | Verdict |
|---|---|---|---|---|
| **arrow / arrow-flight** (arrow-rs) | 60.0.0 (2026-09-15); monthly releases, major at most quarterly ([releases](https://github.com/apache/arrow-rs/releases)) | Foundation; Parquet reader/writer; compute kernels; IPC/Flight | Low-level kernels; no time-series ops; semver-major bumps every ~3 months | Interchange format and Parquet I/O; write few kernels directly |
| **polars** (Rust) | 0.55.2 (2026-08-06); Python 1.44.2 stable, 2.0.0-rc.2 (2026-09-02) makes the streaming engine default ([announcement](https://pola.rs/posts/announcing-polars-2/)) | Best time-series ergonomics: `group_by_dynamic`, `rolling`, `upsample`, `join_asof`, `interpolate`; lazy optimizer; streaming for >RAM data | Rust API explicitly less stable than Python: breaking changes not deprecated first ([versioning](https://docs.pola.rs/development/versioning/)); Polars 2.0 streaming does not guarantee row order for join/group_by unless opted in; Rust crate pairing with 2.0 unverified | **Primary compute engine.** Pin exact version; thin adapter layer; expect a Rust upgrade PR every ~2 months |
| **datafusion** | 55.1.0 (2026-09-11); 56.0.0 planned Oct 2026 ([blog](https://datafusion.apache.org/blog/)) | SQL over Parquet/object stores, streaming execution, UDFs; same engine InfluxDB 3 and GreptimeDB use | No dynamic/rolling time windows built in; SQL-first API clumsy for per-tag pipelines | "SQL over the Parquet cache" and explorer path, plus user SQL checks; not for core kernels |

**How to build checks.** Build checks as **Polars lazy expressions** (composable, optimized,
vectorized, run on the streaming engine), drop to **Arrow compute kernels** only for hot
numeric loops Polars cannot express (custom changepoint scoring), and expose **DataFusion
UDFs** only where a user writes SQL. Pattern: each check is a Rust struct implementing
`fn plan(&self, lf: LazyFrame, cfg) -> LazyFrame` producing a standard
`(tag, window_start, window_end, score, flag, detail)` frame, so checks are testable and
composable, and the same struct can register itself as a Polars expression plugin via
**pyo3-polars 0.28.0**.

**Statistics / ML crates (verified on crates.io):**

| Crate | Version | Use |
|---|---|---|
| `statrs` | 0.19.1 (2026-08-11) | Distributions, hypothesis tests, quantiles |
| `ndarray` | 0.17.2 | Dense matrices where Arrow buffers aren't convenient |
| `augurs` (Grafana) | 0.10.2, MIT/Apache-2.0; features `changepoint`, `seasons`, `outlier` (MAD, DBSCAN), `mstl`, `ets`, `prophet`, `clustering`, `dtw` ([docs](https://docs.rs/augurs), [repo](https://github.com/grafana/augurs)) | Best single crate for seasonality detection, changepoints, MSTL, outliers; Python bindings too |
| `changepoint` | 0.15.0 (2025-10-20) | Bayesian online changepoint detection (used under augurs) |
| `rustfft` | 6.4.1 | Spectral checks (periodicity, noise floor) |
| `linfa` | 0.8.1 (2025-12-23) | Classical ML (clustering, PCA) |
| `smartcore` | 0.6.14 (2026-08-26) | Alternative classical ML; pure Rust |
| `candle-core` | 0.11.0 (2026-06-26) | In-process NN inference if learned detectors ship |
| `ort` | 2.0.0-rc.13 (2026-07-28; ONNX Runtime 1.28) | ONNX inference; still RC, pin exactly |

Recommendation: `statrs + augurs + rustfft` now; `ort` only when a real ONNX model exists.

## 3. Rust ↔ Python bridging

| Approach | Versions | Pros | Cons |
|---|---|---|---|
| **PyO3 + maturin, wheel** | PyO3 0.29.2 (2026-08-28), maturin 1.15.0 (2026-08-24); free-threaded builds since PyO3 0.23 ([guide](https://pyo3.rs/main/free-threading.html), [maturin](https://www.maturin.rs/bindings)) | Zero-copy via Arrow PyCapsule interface (`__arrow_c_stream__`), one process, one install artifact, simplest air-gap story; `pyo3-polars` 0.28.0 for `PyDataFrame`/`PyLazyFrame` and expression plugins; `pyo3-arrow` 0.19.0 for PyCapsule conversions with pyarrow, polars ≥1.2, pandas ≥2.2 ([pyo3-arrow](https://docs.rs/pyo3-arrow), [arro3](https://kylebarron.dev/arro3/latest/)) | Rust panics/OOM take the Python process down; must release the GIL around long compute; pyo3-polars pins a polars version |
| **Separate Rust service (tonic 0.14.6 / axum 0.8.9) + Arrow Flight 60** | ([arrow-flight](https://crates.io/crates/arrow-flight)) | Process isolation, independent scaling, language-agnostic, can host DataFusion Flight SQL | Second daemon to package/secure/observe; every window crosses a socket; mTLS config |

**Verdict:** ship the core as a **PyO3 wheel** now (`abi3` wheel for 3.10–3.13 plus a 3.14t
wheel if free-threading is supported), design the Rust API around `RecordBatch`/`LazyFrame`
in and out so the same crate can later be wrapped in an axum/tonic Flight server without
change. Avoid pyo3-polars version lock-in on the public API by accepting/returning Arrow C
stream capsules (via pyo3-arrow) instead of `PyDataFrame`.

## 4. Python API layer, ORM, jobs

| Area | Recommendation | Version | Alternatives |
|---|---|---|---|
| Web framework | **FastAPI** | 0.141.1 (2026-07-29); requires Pydantic ≥2.7; Starlette 1.6 ([release notes](https://fastapi.tiangolo.com/release-notes/)) | Litestar 2.24.0 (excellent DTO/msgspec story, smaller ecosystem); Django Ninja 1.7.1 |
| Validation/settings | Pydantic 2.13.5 + pydantic-settings | | |
| ORM | **SQLAlchemy 2.0 async** (2.0.54) with psycopg 3.3.6 or asyncpg 0.31 | | SQLModel 0.0.42 (still 0.0.x, avoid for core models); Piccolo 1.36; Tortoise 1.1.8 |
| Migrations | Alembic 1.20.0 | | |
| ASGI server | uvicorn 0.53 or granian 2.8.3 | | |
| Jobs/scheduler | **Procrastinate** 3.9.0 — Postgres-only (LISTEN/NOTIFY + `FOR UPDATE SKIP LOCKED`), async+sync, periodic tasks, transactional enqueue ([docs](https://procrastinate.readthedocs.io/)) | | pgqueuer 1.4.0; Taskiq 0.12.6 (needs Redis/RabbitMQ/NATS); Dramatiq 2.2.1; Celery 5.6.3; arq 0.28 (maintenance-only); APScheduler 3.11.3 (in-process cron only) |
| Durable workflows (optional) | Temporal Python SDK 1.33.0 or Hatchet 1.40.3 (Postgres-based) | | Prefect 3.8.6 / Dagster 1.13.23 too heavy as an embedded dependency |

**Why Procrastinate for air-gapped installs:** removes Redis/RabbitMQ from the bill of
materials, the queue is backed up with the metadata DB, jobs enqueue in the same transaction
as run records, and workers are plain Python processes that can also host the plugin
sandbox. If long-running, resumable "profile 100k tags over 5 years" runs with visible
progress are needed later, Hatchet (one Go binary + Postgres) is the lighter durable
execution option; Temporal is the most mature but adds its own server, schema and UI.

## 5. Storage

| System | Status/Licence (verified) | Fit for **results/metrics** | Fit for **raw TS cache** |
|---|---|---|---|
| **PostgreSQL + TimescaleDB** | 2.30.1 (2026-09-17). Apache-2 edition lacks compression/hypercore, continuous aggregates, retention policies; those are Community edition under the Timescale License, which forbids offering it *as a service* but does not restrict self-hosted use ([editions](https://www.tigerdata.com/docs/about/latest/timescaledb-editions), [TSL](https://github.com/timescale/timescaledb/blob/master/tsl/LICENSE-TIMESCALE)). Company renamed TigerData in 2025 | **Best.** Results are (tag, window, check, score) rows: hypertable + columnstore compression + continuous aggregates for dashboards; same DB as metadata → one backup | Possible but heavy for 100k tags × years at 1s |
| **Parquet on disk/MinIO + Polars/DataFusion/DuckDB** | `object_store` 0.14.2; DuckDB 1.5.5 (Python) | Poor (no updates) | **Best.** Partition by `source/tag_bucket/year/month`; sorted-by-time row groups; zstd; ~10–30× compression on process data; every engine reads it; trivially air-gapped |
| ClickHouse | 26.8 LTS (2026-09-01), Apache 2 ([changelog](https://clickhouse.com/docs/resources/changelogs/oss/2026)) | Good but overkill | Excellent ad-hoc analytics; another stateful server; `clickhouse` Rust 0.15.2, `clickhouse-connect` 1.8.0 |
| QuestDB | 10.0 (Aug 2026), Apache 2 core; `questdb-rs` 7.0.0, `questdb` py 5.0.0 | Good | Very fast ingest, Postgres wire + ILP; smaller ecosystem |
| InfluxDB 3 | 3.11; Core is MIT/Apache-2, Rust/Arrow/DataFusion/Parquet; Core caps a query plan at 432 Parquet files (~72 h at 10-min blocks) and has no compactor; Enterprise (paid) lifts it ([blog](https://www.influxdata.com/blog/influxdb3-open-source-public-alpha-jan-27/), [Layerbase](https://layerbase.com/blog/influxdb-3-core-72-hour-limit)) | No | **Not viable** as an embedded cache: Core's historical-query limit conflicts with years of history; Enterprise licence cannot be redistributed |
| GreptimeDB | Core Apache 2 with enterprise-gated features; Rust/DataFusion | OK | Reasonable but young for on-prem industrial |
| Arroyo / RisingWave | Streaming SQL engines | Only for real-time checks on live MQTT/Kafka feeds | n/a |

**Recommendation:** Postgres + TimescaleDB Community for metadata + results (document to
customers that the TSL is fine for self-hosted use; keep an Apache-2-only mode that disables
compression/caggs for customers whose legal teams object), Parquet cache for raw data,
DuckDB/DataFusion as query engines over it. Add a ClickHouse *connector* rather than
embedding ClickHouse.

## 6. Connectors

| Source | Access path | Rust | Python | Licensing / notes |
|---|---|---|---|---|
| AVEVA PI | **PI Web API** (REST; batch, streams, `recorded`/`interpolated`/`summary`) is the only cross-platform route; AF SDK is .NET/Windows-only ([PI Web API](https://docs.aveva.com/bundle/pi-web-api-reference/page/help.html), [samples](https://github.com/AVEVA/sample-pi_web_api-common_actions-python)) | `reqwest` | `httpx` (+Kerberos/Basic) | PI OMF for writes only; PI Integrator paid; PI ODBC/JDBC licensed separately |
| AVEVA CONNECT data services | REST (SDS) ([docs](https://docs.aveva.com/bundle/connect-data-services-developer/page/index.html)) | `reqwest` | REST | Cloud only |
| OPC UA | Client | **`async-opcua`** 0.19.0 (2026-07-18, tokio, OPC UA 1.05; fork of `opcua` 0.12) ([crate](https://crates.io/crates/async-opcua)) | `asyncua` 2.0.1 | HDA (history read) support should be verified per server |
| OPC DA (classic) | Only via COM on Windows or gateways | none | `openopc2` 0.1.19 (needs Windows gateway) | Document Matrikon UA Wrapper / Kepware / OPC Router as the supported path |
| Modbus TCP / SunSpec | `tokio-modbus` 0.17.0 | ✔ | `pymodbus` 3.15.0; `pysunspec2` 1.3.6 (Apache 2) | |
| MQTT / Sparkplug B | `rumqttc` 0.25.1; `sparkplug-rs` 0.5.1 | ✔ | `paho-mqtt` 2.1.0 | Sparkplug spec is Eclipse, open |
| IEC 60870-5-104 | `iec104` 0.5.1, `iec60870-5` 0.2.1 (pure Rust), `lib60870` 0.4.0 (bindings) | ✔ | `c104` 2.2.1 (bindings to lib60870) | lib60870-C is **GPLv3 or commercial** — prefer pure-Rust crates |
| IEC 61850 / MMS | libiec61850 (C) | via FFI | `pyiec61850-ng` | **GPLv3**, commercial licence required for a proprietary product ([libiec61850](https://github.com/mz-automation/libiec61850)) |
| DNP3 | `dnp3` 1.6.0 (Step Function I/O) | ✔ | `pydnp3` (2018, dead) | **Non-commercial licence; commercial licence required** ([README](https://github.com/stepfunc/dnp3/blob/main/dnp3/README.md)) |
| IEC 61400-25 (wind) | Maps onto IEC 61850/MMS or OPC XML-DA | – | – | No dedicated open library found (unverified negative); usually reached via SCADA/OPC UA |
| Files | CSV/Parquet/Excel | polars, arrow | polars, pyarrow 25.0.1, openpyxl | |
| SQL (ODBC) | `arrow-odbc` 25.3.0 / `odbc-api` 29.1.0; `connectorx` 0.4.6 | ✔ | `pyodbc` 5.3.0 | Aspen IP.21 (SQLplus ODBC, port 10014) and Canary (ODBC) both come this way |
| Canary | Read (Views) Web API + gRPC + ODBC ([docs](https://help.canarylabs.com/hc/en-us/sections/360004483034-Web-API)) | reqwest/tonic | httpx | Token auth |
| Honeywell PHD | PHD API .NET / OLEDB / OPC HDA only | – | – | Windows-side bridge needed; recommend OPC UA/HDA gateway |
| Ignition | Historian is a SQL DB → read tables directly; 8.3 REST API is config-only ([docs](https://www.docs.inductiveautomation.com/docs/8.3/platform/gateway/openapi)) | sqlx 0.9 | SQLAlchemy | |
| Kafka | `rdkafka` 0.39.0 | ✔ | `confluent-kafka` 2.15.1, `aiokafka` 0.14.0 | |
| Azure IoT Hub / Event Hubs | `azure-eventhub` 5.15.1, `azure-iot-hub` 2.7.0 | – | ✔ | |
| AWS IoT SiteWise | `boto3` 1.43.98: `batch_get_asset_property_value_history` (≤16 entries/req) | – | ✔ | |
| InfluxDB / Timescale / ClickHouse / QuestDB | `influxdb3` 0.2.5 rs, `influxdb3-python` 0.21.0; Postgres wire; `clickhouse-connect` 1.8.0; `questdb` clients | ✔ | ✔ | |
| Cognite CDF | `cognite-sdk` 8.17.0 (async client in v8) | – | ✔ | |
| Databricks / Snowflake | `databricks-sql-connector` 4.5.0, `snowflake-connector-python` 4.7.4 (both return Arrow) | – | ✔ | |

**Connector architecture:** define connectors in **Python** (breadth of SDKs, easy for
partners) returning Arrow C streams; implement the three high-volume ones (Parquet/CSV,
ODBC, OPC UA) in Rust when throughput demands.

## 7. Deployment, licensing, observability

- **Artifacts:** one **docker compose bundle** (app image = Python + Rust wheel; `postgres`
  image with TimescaleDB; optional MinIO) as the default; a **Helm chart** for Kubernetes
  customers; a single static binary is not realistic once Python plugins are a feature, so
  reserve "single binary" for a Rust-only CLI/agent (edge collector).
- **Air-gap pattern:** checksummed tarball with `docker save`d images, the Helm chart, wheels
  for plugins, and a manifest; install from a local registry; no outbound calls at install
  ([Kubernetes air-gap guide](https://kubernetes.io/blog/2023/10/12/bootstrap-an-air-gapped-cluster-with-kubeadm/),
  [GitGuardian example](https://docs.gitguardian.com/self-hosting/installation/airgap-installation-existing-cluster-helm)).
- **Offline licences:** Ed25519-signed licence files verified in-app with the public key,
  machine-fingerprint node-locking, expiry + entitlement claims; Keygen's
  [offline model](https://keygen.sh/docs/choosing-a-licensing-model/offline-licenses/) and
  [air-gapped activation example](https://github.com/keygen-sh/air-gapped-activation-example)
  document the pattern; implementable with `ed25519-dalek` in Rust or `cryptography` in
  Python without a vendor.
- **Observability:** Rust `opentelemetry`/`opentelemetry_sdk`/`opentelemetry-otlp`/
  `opentelemetry-prometheus` all 0.33.0 (2026-09-18) + `tracing-opentelemetry` 0.33 (pairs
  with OTel 0.32; check the matrix when bumping) ([opentelemetry-rust](https://github.com/open-telemetry/opentelemetry-rust));
  Python `opentelemetry-sdk` 1.44.0, `opentelemetry-instrumentation-fastapi` 0.65b0,
  `prometheus-client` 0.26.0. Expose `/metrics` and OTLP; ship optional Grafana dashboards.

## 8. Plugin system for user-written checks

**How others define checks:** Great Expectations 1.23.1 — Python subclasses of
`ColumnExpectation`/`BatchExpectation`; Soda Core 4.24.0 — YAML **SodaCL**
(`missing_count(x) = 0`, user-defined SQL metrics, Python UDF escape hatch)
([SodaCL](https://docs.soda.io/soda-cl-overview/quick-start-sodacl)); dbt — SQL `SELECT`
that returns failing rows, generic tests parameterised from YAML
([dbt](https://docs.getdbt.com/docs/build/data-tests)); Grafana — YAML-provisioned rules
with datasource queries + expression conditions ([Grafana](https://grafana.com/docs/grafana/latest/alerting/set-up/provision-alerting-resources/file-provisioning/));
Timeseer — 100+ built-in "scores" grouped into KPIs, custom checks configured in-product;
DSL not publicly documented.

**Recommended three-tier model:**
1. **YAML DSL** (parameterise built-in Rust checks: thresholds, windows, tag selectors,
   `for each` like SodaCL) — covers 80% of users, no code execution.
2. **SQL checks** over the Parquet cache via DataFusion/DuckDB (dbt-style "rows returned =
   failures") — safe by construction, read-only sessions, statement timeouts.
3. **Python checks**: a decorated function `(polars.DataFrame, config) -> CheckResult`,
   executed in a **separate worker process pool** with cgroup CPU/memory limits, no network
   namespace, read-only FS, seccomp profile; results returned as Arrow over a pipe. This is
   the pragmatic 2026 baseline (bubblewrap/seccomp alone provide no resource limits;
   gVisor/Firecracker are the hardening step for multi-tenant SaaS)
   ([sandbox comparison](https://www.shayon.dev/post/2026/52/lets-discuss-sandbox-isolation/)).
   `RestrictedPython` 8.5 is *not* a security boundary.
4. **WASM lane (optional):** `wasmtime` 48.0.2 / `extism` 1.30.0 with fuel + memory limits
   for compiled plugins (Rust/Go); MicroPython-in-WASM is viable for tiny scripts
   ([Willison, 2026](https://simonwillison.net/2026/Jun/6/micropython-in-a-sandbox/)) but
   cannot run numpy/polars; Pyodide server-side is not supported and had a sandbox escape
   reported in 2026 (CVE-2026-5752, unverified). Do not plan Python-in-WASM as the primary
   plugin path.

## 9. Testing and benchmarking statistical checks

- **Synthetic generators (Rust + Python parity):** parametric generators for flatlines, stuck
  values, spikes, drift, seasonality, regime changes, timestamp jitter, gaps, duplicate
  timestamps, quantisation steps, out-of-order arrival; seed-controlled so Rust (`rand`) and
  Python (`numpy`) produce identical fixtures written to Parquet.
- **Property-based:** `proptest` 1.11.0 in Rust and `hypothesis` 6.168.0 in Python;
  properties such as "injecting a spike of magnitude ≥k·MAD always raises the spike score",
  "checks are invariant to time-zone/offset shifts", "resampling to coarser windows never
  increases missing-ratio below the true value", "results are order-independent under
  streaming".
- **Golden datasets:** version-controlled Parquet fixtures + expected result frames; assert
  with tolerances; include public benchmarks (NAB, UCR anomaly archive, TEP) where licences
  permit.
- **Cross-implementation oracles:** compare Rust results with reference Python (`scipy`,
  `statsmodels`, `ruptures`) in CI at a fixed tolerance.
- **Benchmarks:** `criterion` for kernels; end-to-end harness generating 100k tags × N years
  recording throughput/RSS per engine so regressions (e.g. Polars 2.0's engine switch) are
  caught before release.

## 10. Open risks / things to confirm

- Polars Rust crate churn (breaking every ~2 months, no deprecation cycle); confirm the Rust
  crate version that pairs with Polars 2.0 once GA.
- `ort` remains RC; `tracing-opentelemetry` lags OTel minor versions.
- Commercial-licence obligations if shipping DNP3 (Step Function) or IEC 61850
  (libiec61850): both require paid licences for proprietary products.
- TSL is compatible with self-hosted commercial use but not "as a service"; keep an
  Apache-2-only fallback.
- IEC 61400-25, Honeywell PHD, OPC DA: no viable Linux-native open client found; route
  through OPC UA gateways.

**Key sources:** [arrow-rs releases](https://github.com/apache/arrow-rs/releases) · [Polars versioning](https://docs.pola.rs/development/versioning/) · [Polars 2.0](https://pola.rs/posts/announcing-polars-2/) · [DataFusion blog](https://datafusion.apache.org/blog/) · [augurs](https://github.com/grafana/augurs) · [pyo3-arrow](https://docs.rs/pyo3-arrow) · [PyO3 free-threading](https://pyo3.rs/main/free-threading.html) · [FastAPI release notes](https://fastapi.tiangolo.com/release-notes/) · [Procrastinate](https://procrastinate.readthedocs.io/) · [TimescaleDB editions](https://www.tigerdata.com/docs/about/latest/timescaledb-editions) · [InfluxDB 3 Core limit](https://layerbase.com/blog/influxdb-3-core-72-hour-limit) · [async-opcua](https://crates.io/crates/async-opcua) · [stepfunc/dnp3](https://github.com/stepfunc/dnp3) · [libiec61850](https://github.com/mz-automation/libiec61850) · [PI Web API](https://docs.aveva.com/bundle/pi-web-api-reference/page/help.html) · [Canary Web API](https://help.canarylabs.com/hc/en-us/sections/360004483034-Web-API) · [Keygen offline licensing](https://keygen.sh/docs/choosing-a-licensing-model/offline-licenses/) · [opentelemetry-rust](https://github.com/open-telemetry/opentelemetry-rust) · [SodaCL](https://docs.soda.io/soda-cl-overview/quick-start-sodacl) · [dbt data tests](https://docs.getdbt.com/docs/build/data-tests) · [sandbox isolation](https://www.shayon.dev/post/2026/52/lets-discuss-sandbox-isolation/) · [proptest](https://github.com/proptest-rs/proptest)
