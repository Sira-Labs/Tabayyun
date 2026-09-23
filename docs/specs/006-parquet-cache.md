# Spec 006 — Parquet cache on local disk or S3, coverage

Sprint 7, stories S7-1 and S7-2. Depends on: 001 (coverage table), 002 (worker), 004
(series). Packages: `core/tabayyun-core/src/cache/`, `core/tabayyun-py`, `core/tabayyun-cli`,
`api/src/tabayyun/services/cache.py`, `api/src/tabayyun/services/coverage.py`,
`api/src/tabayyun/services/runs.py`, `api/src/tabayyun/settings.py`, `deploy/`.

## Goal

Raw observations are stored once in an append-only Parquet cache and read back by series
and time range. The cache lives on local disk (development, CLI, single-node installs) or on
any S3-compatible store (RustFS 1.0 on the live system, in the dev bundle and in CI). Every successful upload run writes its series to the cache and records the
range it covered, and a pure planner answers "which parts of this window are missing". Dataset
runs (spec 008) read from the cache; connectors (S9-1) fill the missing ranges.

## User story

As a data engineer, I upload a tag once and can re-run checks on it later, alone or together
with related tags, without uploading it again.

## Interface

Rust (`tabayyun_core::cache`):

```rust
pub struct StoreConfig {            // no env reading in the core; callers pass it in
    pub url: String,                // "/var/lib/tabayyun/cache", "file:///...", "s3://bucket/prefix"
    pub s3_endpoint: Option<String>,        // e.g. "http://srv-captain--rustfs:9000"
    pub s3_region: Option<String>,          // default "us-east-1"
    pub s3_access_key_id: Option<String>,
    pub s3_secret_access_key: Option<String>,
    pub s3_allow_http: bool,                // only for an internal plain-http endpoint
}
pub struct Cache { .. }
impl Cache {
    pub fn open(cfg: &StoreConfig) -> Result<Cache>;
    pub fn write(&self, layer: &str, source_id: &str, frame: &SeriesFrame) -> Result<WriteReport>;
    pub fn read(&self, layer: &str, source_id: &str, series_ids: &[&str],
                start_ns: i64, end_ns: i64) -> Result<Vec<SeriesFrame>>;
}
pub struct WriteReport { pub files: Vec<String>, pub rows: usize, pub start_ns: i64, pub end_ns: i64 }
```

`Cache` owns a small current-thread Tokio runtime and exposes a synchronous API; the Python
bindings release the GIL around it.

Layout (ADR-0003, architecture "Storage layout"):

```
{root}/{layer}/{source_id}/{bucket:02x}/{yyyy}/{mm}/part-{write_ns:019}-{uuid}.parquet
```

`layer` is `raw` (corrected layers follow in sprint 11). `bucket` = FNV-1a 64 of `series_id`
mod 64, so one listing covers a bounded number of series. `yyyy/mm` is the UTC month of the
rows; one write that spans months produces one file per month.

File schema: `series_id` utf8, `ts` timestamp[ns, UTC], `value` float64 (NaN = null),
`quality` uint8 (good 0, uncertain 1, bad 2, estimated 3), `ingest_ts` timestamp[ns, UTC]
nullable. Rows sorted by `(series_id, ts)`, row groups of at most 1 048 576 rows, zstd level
3, column statistics on. (The architecture's typed value columns `value_i64`, `value_bool`,
`value_str` arrive when the core handles non-float series; files carry the schema version in
their key-value metadata `tabayyun.schema = 1`.)

Python bindings (`tabayyun_core`):

```python
cache_write(store: dict, layer: str, source_id: str, series_id: str, table) -> dict  # WriteReport
cache_read(store: dict, layer: str, source_id: str, series_ids: list[str],
           start_ns: int, end_ns: int) -> dict[str, pyarrow.Table]   # ts, value, quality[, ingest_ts]
```

API settings (environment, `TABAYYUN_` prefix):

| Variable | Default | Meaning |
|---|---|---|
| `TABAYYUN_CACHE_URL` | `./data/cache` | local path or `s3://bucket/prefix`; replaces the unused `cache_dir` |
| `TABAYYUN_S3_ENDPOINT` | unset | S3 endpoint URL (unset = AWS) |
| `TABAYYUN_S3_REGION` | `us-east-1` | |
| `TABAYYUN_S3_ACCESS_KEY_ID` | unset | required when the URL is `s3://` |
| `TABAYYUN_S3_SECRET_ACCESS_KEY` | unset | secret; prod refuses a placeholder |
| `TABAYYUN_S3_ALLOW_HTTP` | `false` | allow `http://` endpoints (internal network only) |

Coverage (`api/src/tabayyun/services/coverage.py`): rows in the existing `coverage` table
`(series_id, layer, range_start, range_end, rows, written_at)`, one per write.

```python
def missing_ranges(covered: list[tuple[int, int]], start_ns: int, end_ns: int) -> list[tuple[int, int]]
```

CLI: `tabayyun cache write|read|bench` with `--store` (path or `s3://`) and the S3 options as
flags or the same environment variables.

## Behaviour

1. `Cache::open` parses the URL: a bare path or `file://` opens a local store rooted there
   (created if missing); `s3://bucket/prefix` builds an S3 store from the config. Any other
   scheme, or `s3://` without credentials, is `Error::InvalidParams` naming the field.
2. `write` sorts and normalises the frame (as `SeriesFrame::normalized`), splits it by UTC
   month, writes each part as one immutable object (upload of a complete buffer, never an
   in-place append) and returns the keys, the row count and `[first_ts, last_ts + 1)`. An
   empty frame writes nothing and returns `rows = 0`.
3. `read` lists only the bucket prefixes of the requested series and the months overlapping
   `[start_ns, end_ns)`, skips row groups whose `series_id` or `ts` statistics exclude the
   request, filters rows, and merges parts. When the same `(series_id, ts)` appears in more
   than one file, the row from the file with the larger `write_ns` wins (re-uploads correct
   the cache without rewriting files). Each requested series is returned, empty when absent.
4. Store errors (network, permissions, missing bucket) surface as `Error::Cache(message)`
   with the object key. Each object is one PUT, so no object is ever half written, but a
   write that spans months is *not* atomic: if a later month's PUT fails, the earlier
   months' objects stay visible. The cache listing is therefore never evidence of coverage;
   only `coverage` rows are, and a row is recorded only after every PUT of the write
   succeeded (step 5). Orphaned objects from a failed write are harmless: a retry writes the
   same rows again with a larger `write_ns`, and last-write-wins makes the duplicates
   invisible. Compaction (S13-5) removes them.
5. After an upload run succeeds (spec 002 flow), the worker writes the parsed series to the
   cache under the `Uploads` source and, once the whole write succeeded, adds a `coverage`
   row in a short transaction after the completion transaction.
   A cache failure does not fail the run: the run succeeds, `stats.cache` is
   `{"written": false, "error": "..."}` and the worker logs `cache.write_failed`; on success
   `stats.cache` is `{"written": true, "rows": n, "files": k}`.
6. `missing_ranges` coalesces the covered half-open ranges and returns the gaps inside
   `[start, end)` in order; an empty list means fully covered.
7. With `TABAYYUN_ENV=prod`, a set `TABAYYUN_S3_SECRET_ACCESS_KEY` that looks like a
   placeholder is refused at startup (same rule as the other secrets); an `s3://` cache URL
   without both keys is refused too.

## Acceptance criteria

- [x] Round trip on the local store: write two series over three months, read a sub-range of
      one, get exactly the rows in range, sorted, with values, quality and ingest times intact.
- [x] A second write of overlapping timestamps with different values is read back with the
      newer values; untouched timestamps keep the old ones.
- [x] Reads prune: a read of one series for one month opens only that bucket and month
      (asserted through the read statistics, see Implementation edits).
- [x] The same round trip passes against an S3 endpoint in CI (RustFS service container,
      pinned to the live version, test enabled by `TABAYYUN_TEST_S3_URL`). (CI run 85 on
      94643e9: rust core and python api jobs green against RustFS 1.0.0.)
- [x] `tabayyun cache bench --rows 10000000` writes and reads 10 M rows; the numbers are
      recorded in this spec (Benchmarks).
- [x] An upload run on the live system writes its series to the RustFS bucket and a
      `coverage` row; `stats.cache.written` is true. A run with the store unreachable still
      succeeds with `stats.cache.written = false`. (Live 2026-09-23 after PR #31, release
      run 31: an hourly CSV of 720 rows from 25 Nov to 24 Dec 2023 uploaded as `cache-check`
      succeeded with `stats.cache = {written: true, rows: 720, files: 2}`, one object per
      month; the coverage row is committed in the same transaction as `stats.cache`. The
      unreachable-store half is covered by `tests/db/test_runs_cache.py` in CI rather than
      by breaking the live store.)
- [x] `missing_ranges` unit tests pass (empty, full, gaps at both ends, overlapping and
      touching ranges).
- [x] `make lint` and `make test` pass; the wheel still builds for Python 3.11+.

## Test cases

Unit (`tabayyun-core`, `cache::tests`): `round_trip_local`, `month_split`,
`last_write_wins`, `prunes_buckets_and_months`, `rejects_unknown_scheme`,
`s3_round_trip` (ignored unless `TABAYYUN_TEST_S3_URL` is set).
Bindings (`core/tabayyun-py/tests`): `test_cache_round_trip_pyarrow`.
API (`api/tests`): `test_missing_ranges.py`; `tests/db/test_runs_cache.py`
(upload run writes the cache and a coverage row; unreachable store still succeeds).

## Deployment

- Live: a CapRover app running RustFS 1.0 (Apache-2.0), pinned to an exact version tag,
  persistent directory at `/data`, no public domain for the S3 API. The worker gets
  `TABAYYUN_CACHE_URL=s3://tabayyun-cache`, the internal endpoint
  `http://srv-captain--rustfs:9000` with `TABAYYUN_S3_ALLOW_HTTP=true`, and a dedicated
  access key whose policy allows only that bucket. The owner creates the bucket and the key;
  `deploy/README.md` lists the steps. The api does not need the cache until the chart
  endpoints (sprint 9).
- Verified 2026-09-23 before implementation: from inside the worker container, pyarrow wrote,
  listed, read and deleted a Parquet object in `tabayyun-cache` over
  `http://srv-captain--rustfs:9000` with default AWS-SDK settings (no checksum workaround).
  Port 9000 is the S3 API; 9001 is the console.
- Dev and CI: `deploy/compose.dev.yaml` gains the same RustFS image with S3 on port 9000; CI
  runs it as a service container for the S3 test, so tests exercise the store that runs live.
- History (23 Sep 2026): the first live setup used an existing MinIO. Its community builds and
  images ended in October 2025 and the repository was archived in April 2026; that MinIO
  also predated 2025 and rejected uploads from AWS-SDK clients using the 2025 streamed
  checksums (`411 MissingContentLength`) unless they set
  `AWS_REQUEST_CHECKSUM_CALCULATION=WHEN_REQUIRED`. The cache was still empty, so the store
  was replaced by RustFS 1.0 (GA 16 Sep 2026) the same day; the cache holds only rebuildable
  copies (ADR-0003), so a later store change is a configuration change plus re-uploads.

## Benchmarks

`tabayyun cache bench` (release build, one sandbox VM, RustFS 1.0.0 in Docker on the same
host), 2026-09-23. Rows are synthetic 1 s samples; generation is not timed.

| Store | Rows × series | Files | Write | Read |
|---|---|---|---|---|
| local disk | 10 M × 1 | 4 | 2.14 s (4.7 M rows/s) | 2.14 s (4.7 M rows/s) |
| RustFS (S3) | 10 M × 1 | 4 | 2.26 s (4.4 M rows/s) | 1.85 s (5.4 M rows/s) |
| RustFS (S3) | 10 M as 1 000 × 10 k | 1 000 | 9.23 s (1.1 M rows/s) | 2.12 s (4.7 M rows/s) |

About 11 bytes per row on disk for noisy synthetic values. Writes are one sequential PUT per
series and month, so many small series pay per-request latency; connectors (S9-1) should
batch series per request cycle or write concurrently.

## Implementation edits

- Reads keep the listed `Path`s instead of re-parsing their string form: `Path::from` would
  percent-encode an already encoded key again, so ids with `#`, `%`, spaces or non-ASCII
  (allowed as path segments) could not be read back. Found in review; covered by a test on
  both stores.
- Pruning is asserted through `Cache::read_with_stats` (`prefixes_listed`, `files_read`,
  `row_groups_read`, `row_groups_skipped`) instead of a counting store wrapper: the numbers
  are also useful in logs, and a wrapper would re-implement the whole `ObjectStore` trait.
- Objects are fetched whole and row groups pruned by statistics after the fetch; ranged
  reads (fetch only matching row groups) wait until files grow large enough to matter.
- The cache write happens after the completion transaction, not inside it: the write is
  network I/O and the transaction holds the per-series advisory lock (spec 003). The
  coverage row and `stats.cache` are written in a short follow-up transaction.
- The cache keys a series by its database UUID under its source's UUID, not by the upload's
  external id, so dataset runs (spec 008) read by the ids they already hold.
- A second upload whose range starts at the same instant widens the existing coverage row
  (`ON CONFLICT` on the primary key) instead of failing.
- `object_store` 0.14.2: the cache fetches objects and parses the bytes itself, so it does not
  use `parquet`'s own `object_store` integration and is not tied to its version. 0.13 was tried
  first and failed `cargo audit` in CI (its `quick-xml` 0.39 has RUSTSEC-2026-0194/0195; 0.14.1+
  uses 0.41). `with_client_options` replaces the whole option set, so `allow_http` is set on
  the client options, a bug found by the RustFS test.
- Store errors are summarised as `<what>: <hint>: <root cause>` (for example
  `access denied (check the key and its bucket policy)`, `Connection refused`), because that
  line is what an operator reads in `stats.cache.error`; `deploy/caprover.md` lists them.
- `TABAYYUN_CACHE_DIR` stays accepted as an alias of `TABAYYUN_CACHE_URL`: the live api app
  and older compose files still set it.
- CI creates the test bucket with the runner's `aws` CLI; `make dev-bucket` does the same for
  the dev RustFS.
- The built extension module `_native.abi3.so` had been committed since sprint 2; it is now
  ignored and built by maturin only.

## Out of scope

- Connectors and `fetch_window` jobs that fill missing ranges (S9-1).
- Compaction of small files and retention (sprint 13, S13-5 runbook).
- Corrected layers `corrected/v{n}` (S11-2).
- API read access for charts (sprint 9, S9-5).
