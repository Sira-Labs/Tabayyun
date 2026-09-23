//! Parquet cache of raw observations on local disk or S3-compatible storage (spec 006).
//!
//! Layout (ADR-0003):
//! `{root}/{layer}/{source_id}/{bucket:02x}/{yyyy}/{mm}/part-{write_ns:019}-{rand:016x}.parquet`
//! where `bucket` is FNV-1a 64 of the series id mod 64 and `yyyy/mm` the UTC month of the rows.
//! Files are immutable and written with one PUT each; a later write of the same
//! `(series_id, ts)` wins on read because its `write_ns` is larger, so re-uploads correct the
//! cache without rewriting files.
//!
//! This module does I/O and therefore lives outside `checks` (ADR-0015). It owns a small Tokio
//! runtime and exposes a synchronous API; callers (CLI, Python bindings) never see async.

use crate::error::{Error, Result};
use crate::frame::{SeriesFrame, SeriesMeta};
use crate::quality::Quality;
use arrow::array::{
    Array, ArrayRef, Float64Array, RecordBatch, StringArray, TimestampNanosecondArray, UInt8Array,
};
use arrow::datatypes::{DataType, Field, Schema, TimeUnit};
use bytes::Bytes;
use futures::{StreamExt, TryStreamExt};
use object_store::aws::AmazonS3Builder;
use object_store::local::LocalFileSystem;
use object_store::path::Path;
use object_store::{BackoffConfig, ClientOptions, ObjectStore, ObjectStoreExt, PutPayload, RetryConfig};
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use parquet::arrow::ArrowWriter;
use parquet::basic::{Compression, ZstdLevel};
use parquet::file::metadata::{KeyValue, RowGroupMetaData};
use parquet::file::properties::{EnabledStatistics, WriterProperties};
use parquet::file::statistics::Statistics;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::atomic::{AtomicI64, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

/// Number of tag buckets per source; one listing covers the series of one bucket and month.
pub const BUCKETS: u64 = 64;
/// Upper bound of rows per Parquet row group.
pub const ROW_GROUP_ROWS: usize = 1 << 20;
/// Schema version written into every file's key-value metadata.
pub const SCHEMA_VERSION: &str = "1";
/// Concurrent object reads per `read` call.
const READ_CONCURRENCY: usize = 8;

/// Where the cache lives. The core reads no environment; callers pass this in.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct StoreConfig {
    /// A local path, `file:///path`, or `s3://bucket[/prefix]`.
    pub url: String,
    #[serde(default)]
    pub s3_endpoint: Option<String>,
    #[serde(default)]
    pub s3_region: Option<String>,
    #[serde(default)]
    pub s3_access_key_id: Option<String>,
    #[serde(default)]
    pub s3_secret_access_key: Option<String>,
    /// Allow a plain-http endpoint (internal network only).
    #[serde(default)]
    pub s3_allow_http: bool,
}

/// What one `write` stored.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct WriteReport {
    pub files: Vec<String>,
    pub rows: usize,
    /// First timestamp written (ns), 0 when nothing was written.
    pub start_ns: i64,
    /// Last timestamp written + 1 ns, 0 when nothing was written.
    pub end_ns: i64,
}

/// How much of the store one `read` touched; tests assert pruning with it and runs can log it.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct ReadStats {
    pub prefixes_listed: usize,
    pub files_read: usize,
    pub row_groups_read: usize,
    pub row_groups_skipped: usize,
    pub rows: usize,
}

/// A Parquet cache bound to one store.
pub struct Cache {
    store: Arc<dyn ObjectStore>,
    /// Key prefix inside the store (the part after the bucket for `s3://bucket/prefix`).
    root: String,
    runtime: tokio::runtime::Runtime,
}

impl std::fmt::Debug for Cache {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Cache").field("store", &self.store.to_string()).field("root", &self.root).finish()
    }
}

fn cache_err(context: &str, e: impl std::fmt::Display) -> Error {
    Error::Cache(format!("{context}: {e}"))
}

/// A store error as one readable line: what failed, a hint for the common causes, and the
/// innermost cause (object_store's own message nests it behind the request URL).
fn store_err(context: &str, e: object_store::Error) -> Error {
    use object_store::Error as E;
    let hint = match &e {
        E::PermissionDenied { .. } | E::Unauthenticated { .. } => {
            "access denied (check the key and its bucket policy)"
        }
        // A write or listing finds no bucket; a read of a listed object finds it gone.
        E::NotFound { .. } if context.starts_with("get") => "object not found",
        E::NotFound { .. } => "not found (does the bucket exist?)",
        _ => "store request failed",
    };
    let mut root: &dyn std::error::Error = &e;
    while let Some(next) = root.source() {
        root = next;
    }
    Error::Cache(format!("{context}: {hint}: {root}"))
}

fn invalid(field: &str, reason: impl Into<String>) -> Error {
    Error::InvalidParams { check: format!("cache.{field}"), reason: reason.into() }
}

impl Cache {
    /// Open the store named by `cfg.url`. Local directories are created when missing.
    pub fn open(cfg: &StoreConfig) -> Result<Cache> {
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .thread_name("tabayyun-cache")
            .enable_all()
            .build()
            .map_err(|e| cache_err("runtime", e))?;
        let url = cfg.url.trim();
        if url.is_empty() {
            return Err(invalid("url", "empty cache url"));
        }
        let (store, root): (Arc<dyn ObjectStore>, String) = if let Some(rest) = url.strip_prefix("s3://") {
            let (bucket, prefix) = rest.split_once('/').unwrap_or((rest, ""));
            if bucket.is_empty() {
                return Err(invalid("url", "s3:// url without a bucket"));
            }
            let key_id = cfg.s3_access_key_id.as_deref().filter(|s| !s.is_empty());
            let secret = cfg.s3_secret_access_key.as_deref().filter(|s| !s.is_empty());
            let (Some(key_id), Some(secret)) = (key_id, secret) else {
                return Err(invalid("s3_access_key_id", "an s3:// cache needs an access key id and secret"));
            };
            let mut builder = AmazonS3Builder::new()
                .with_bucket_name(bucket)
                .with_region(cfg.s3_region.clone().unwrap_or_else(|| "us-east-1".into()))
                .with_access_key_id(key_id)
                .with_secret_access_key(secret)
                // Path-style requests: self-hosted stores (RustFS, SeaweedFS) route by path.
                .with_virtual_hosted_style_request(false)
                // `with_client_options` replaces the whole option set, so `allow_http` must be
                // set here; a separate `with_allow_http` before it would be silently reset.
                .with_client_options(
                    ClientOptions::new()
                        .with_allow_http(cfg.s3_allow_http)
                        .with_connect_timeout(Duration::from_secs(5))
                        .with_timeout(Duration::from_secs(60)),
                )
                .with_retry(RetryConfig {
                    backoff: BackoffConfig::default(),
                    max_retries: 3,
                    retry_timeout: Duration::from_secs(30),
                });
            if let Some(ep) = cfg.s3_endpoint.as_deref().filter(|s| !s.is_empty()) {
                builder = builder.with_endpoint(ep);
            }
            let store = builder.build().map_err(|e| cache_err("s3 config", e))?;
            (Arc::new(store), prefix.trim_matches('/').to_string())
        } else {
            let path = url.strip_prefix("file://").unwrap_or(url);
            if url.contains("://") && !url.starts_with("file://") {
                return Err(invalid(
                    "url",
                    format!("unsupported cache url scheme in `{url}` (use a path, file:// or s3://)"),
                ));
            }
            std::fs::create_dir_all(path).map_err(|e| cache_err(&format!("create {path}"), e))?;
            let store = LocalFileSystem::new_with_prefix(path).map_err(|e| cache_err("local store", e))?;
            (Arc::new(store), String::new())
        };
        Ok(Cache { store, root, runtime })
    }

    fn key(&self, rest: &str) -> String {
        if self.root.is_empty() {
            rest.to_string()
        } else {
            format!("{}/{rest}", self.root)
        }
    }

    /// Write one series. The frame is sorted and de-duplicated first; each UTC month becomes
    /// one immutable object. An empty frame writes nothing.
    pub fn write(&self, layer: &str, source_id: &str, frame: &SeriesFrame) -> Result<WriteReport> {
        check_segment("layer", layer)?;
        check_segment("source_id", source_id)?;
        let (frame, _) = frame.normalized();
        let n = frame.len();
        if n == 0 {
            return Ok(WriteReport { files: vec![], rows: 0, start_ns: 0, end_ns: 0 });
        }
        let series_id = frame.meta.id.as_str();
        let bucket = bucket_of(series_id);
        let write_ns = next_write_ns();
        let mut parts: Vec<(String, Bytes)> = Vec::new();
        let mut s = 0usize;
        while s < n {
            let (y, m) = year_month(frame.ts[s]);
            let month_end = month_start_ns(y, m + 1);
            let mut e = s;
            while e < n && frame.ts[e] < month_end {
                e += 1;
            }
            let key = self.key(&format!(
                "{layer}/{source_id}/{bucket:02x}/{y:04}/{m:02}/part-{write_ns:019}-{:016x}.parquet",
                random_u64()
            ));
            parts.push((key, encode_part(&frame, s, e)?));
            s = e;
        }
        let store = self.store.clone();
        let keys: Vec<String> = parts.iter().map(|(k, _)| k.clone()).collect();
        self.runtime.block_on(async move {
            for (key, body) in parts {
                store
                    .put(&Path::from(key.as_str()), PutPayload::from_bytes(body))
                    .await
                    .map_err(|e| store_err(&format!("put {key}"), e))?;
            }
            Ok::<_, Error>(())
        })?;
        Ok(WriteReport { files: keys, rows: n, start_ns: frame.ts[0], end_ns: frame.ts[n - 1] + 1 })
    }

    /// Read series `[start_ns, end_ns)`. Every requested id is returned in request order, with
    /// an empty frame when the cache holds nothing for it.
    pub fn read(
        &self,
        layer: &str,
        source_id: &str,
        series_ids: &[&str],
        start_ns: i64,
        end_ns: i64,
    ) -> Result<Vec<SeriesFrame>> {
        self.read_with_stats(layer, source_id, series_ids, start_ns, end_ns).map(|(f, _)| f)
    }

    /// [`Cache::read`] plus the listing and pruning counters.
    pub fn read_with_stats(
        &self,
        layer: &str,
        source_id: &str,
        series_ids: &[&str],
        start_ns: i64,
        end_ns: i64,
    ) -> Result<(Vec<SeriesFrame>, ReadStats)> {
        check_segment("layer", layer)?;
        check_segment("source_id", source_id)?;
        let mut stats = ReadStats::default();
        let empty = |ids: &[&str]| {
            ids.iter()
                .map(|id| SeriesFrame::with_default_quality(SeriesMeta::new(*id), vec![], vec![]))
                .collect::<Result<Vec<_>>>()
        };
        if series_ids.is_empty() || end_ns <= start_ns {
            return Ok((empty(series_ids)?, stats));
        }
        let wanted: BTreeSet<&str> = series_ids.iter().copied().collect();
        let buckets: BTreeSet<u64> = wanted.iter().map(|id| bucket_of(id)).collect();
        let prefixes: Vec<String> = buckets
            .iter()
            .flat_map(|b| {
                months_between(start_ns, end_ns)
                    .into_iter()
                    .map(move |(y, m)| format!("{layer}/{source_id}/{b:02x}/{y:04}/{m:02}/"))
            })
            .map(|p| self.key(&p))
            .collect();
        stats.prefixes_listed = prefixes.len();

        let store = self.store.clone();
        let objects: Vec<(String, Bytes)> = self.runtime.block_on(async move {
            let mut keys = Vec::new();
            for prefix in &prefixes {
                let listed: Vec<_> = store
                    .list(Some(&Path::from(prefix.as_str())))
                    .try_collect()
                    .await
                    .map_err(|e| store_err(&format!("list {prefix}"), e))?;
                // Keep the listed `Path`s: they are already encoded, and `Path::from` on their
                // string form would percent-encode reserved characters (`#`, `%`, ...) twice.
                keys.extend(
                    listed.into_iter().map(|m| m.location).filter(|p| p.as_ref().ends_with(".parquet")),
                );
            }
            futures::stream::iter(keys)
                .map(|path| {
                    let store = store.clone();
                    async move {
                        let key = path.to_string();
                        let body = store
                            .get(&path)
                            .await
                            .map_err(|e| store_err(&format!("get {key}"), e))?
                            .bytes()
                            .await
                            .map_err(|e| store_err(&format!("get {key}"), e))?;
                        Ok::<_, Error>((key, body))
                    }
                })
                .buffer_unordered(READ_CONCURRENCY)
                .try_collect()
                .await
        })?;
        stats.files_read = objects.len();

        // (ts, write_ns) -> row, per series; a later write of the same ts wins.
        let mut rows: BTreeMap<&str, Vec<Row>> = wanted.iter().map(|id| (*id, Vec::new())).collect();
        for (key, body) in objects {
            let write_ns = write_ns_of(&key);
            decode_part(&key, body, write_ns, &wanted, start_ns, end_ns, &mut rows, &mut stats)?;
        }

        let mut frames = Vec::with_capacity(series_ids.len());
        for id in series_ids {
            let mut rs = rows.get(id).cloned().unwrap_or_default();
            rs.sort_by_key(|r| (r.ts, r.write_ns));
            let mut kept: Vec<Row> = Vec::with_capacity(rs.len());
            for r in rs {
                match kept.last_mut() {
                    Some(last) if last.ts == r.ts => *last = r,
                    _ => kept.push(r),
                }
            }
            stats.rows += kept.len();
            let all_ingest = !kept.is_empty() && kept.iter().all(|r| r.ingest_ts.is_some());
            let frame = SeriesFrame::new(
                SeriesMeta::new(*id),
                kept.iter().map(|r| r.ts).collect(),
                kept.iter().map(|r| r.value).collect(),
                kept.iter().map(|r| r.quality).collect(),
            )?;
            frames.push(if all_ingest {
                frame.with_ingest_ts(kept.iter().map(|r| r.ingest_ts.unwrap_or_default()).collect())?
            } else {
                frame
            });
        }
        Ok((frames, stats))
    }
}

#[derive(Debug, Clone, Copy)]
struct Row {
    ts: i64,
    write_ns: i64,
    value: f64,
    quality: Quality,
    ingest_ts: Option<i64>,
}

/// Path segments come from ids the API controls, but a `/` or `..` would escape the layout.
fn check_segment(field: &str, v: &str) -> Result<()> {
    if v.is_empty() || v.contains('/') || v.contains('\\') || v == "." || v == ".." {
        return Err(invalid(field, format!("`{v}` is not a valid path segment")));
    }
    Ok(())
}

fn file_schema() -> Arc<Schema> {
    let ts = DataType::Timestamp(TimeUnit::Nanosecond, Some("UTC".into()));
    Arc::new(Schema::new(vec![
        Field::new("series_id", DataType::Utf8, false),
        Field::new("ts", ts.clone(), false),
        Field::new("value", DataType::Float64, false),
        Field::new("quality", DataType::UInt8, false),
        Field::new("ingest_ts", ts, true),
    ]))
}

/// Encode rows `[s, e)` of a normalised frame as one Parquet object.
fn encode_part(frame: &SeriesFrame, s: usize, e: usize) -> Result<Bytes> {
    let schema = file_schema();
    let len = e - s;
    let ingest: Option<Vec<i64>> = frame.ingest_ts.as_ref().map(|v| v[s..e].to_vec());
    let columns: Vec<ArrayRef> = vec![
        Arc::new(StringArray::from(vec![frame.meta.id.as_str(); len])),
        Arc::new(TimestampNanosecondArray::from(frame.ts[s..e].to_vec()).with_timezone("UTC")),
        Arc::new(Float64Array::from(frame.values[s..e].to_vec())),
        Arc::new(UInt8Array::from(frame.quality[s..e].iter().map(|q| q.as_u8()).collect::<Vec<_>>())),
        Arc::new(
            match ingest {
                Some(v) => TimestampNanosecondArray::from(v),
                None => TimestampNanosecondArray::from(vec![None::<i64>; len]),
            }
            .with_timezone("UTC"),
        ),
    ];
    let batch = RecordBatch::try_new(schema.clone(), columns)?;
    let props = WriterProperties::builder()
        .set_compression(Compression::ZSTD(ZstdLevel::try_new(3).map_err(|e| cache_err("zstd level", e))?))
        .set_max_row_group_row_count(Some(ROW_GROUP_ROWS))
        .set_statistics_enabled(EnabledStatistics::Chunk)
        .set_key_value_metadata(Some(vec![KeyValue::new(
            "tabayyun.schema".into(),
            SCHEMA_VERSION.to_string(),
        )]))
        .build();
    let mut buf = Vec::new();
    let mut writer =
        ArrowWriter::try_new(&mut buf, schema, Some(props)).map_err(|e| cache_err("parquet writer", e))?;
    writer.write(&batch).map_err(|e| cache_err("parquet write", e))?;
    writer.close().map_err(|e| cache_err("parquet close", e))?;
    Ok(Bytes::from(buf))
}

/// Whether a row group can hold wanted rows, judged by its `series_id` and `ts` statistics.
/// Missing statistics mean "maybe".
fn row_group_may_match(rg: &RowGroupMetaData, wanted: &BTreeSet<&str>, start_ns: i64, end_ns: i64) -> bool {
    for col in rg.columns() {
        match (col.column_path().string().as_str(), col.statistics()) {
            ("ts", Some(Statistics::Int64(s))) => {
                if let (Some(lo), Some(hi)) = (s.min_opt(), s.max_opt()) {
                    if *hi < start_ns || *lo >= end_ns {
                        return false;
                    }
                }
            }
            ("series_id", Some(Statistics::ByteArray(s))) => {
                if let (Some(lo), Some(hi)) = (s.min_opt(), s.max_opt()) {
                    let (lo, hi) = (String::from_utf8_lossy(lo.data()), String::from_utf8_lossy(hi.data()));
                    if !wanted.iter().any(|id| *id >= lo.as_ref() && *id <= hi.as_ref()) {
                        return false;
                    }
                }
            }
            _ => {}
        }
    }
    true
}

#[allow(clippy::too_many_arguments)]
fn decode_part<'a>(
    key: &str,
    body: Bytes,
    write_ns: i64,
    wanted: &BTreeSet<&'a str>,
    start_ns: i64,
    end_ns: i64,
    rows: &mut BTreeMap<&'a str, Vec<Row>>,
    stats: &mut ReadStats,
) -> Result<()> {
    let ctx = |e: &dyn std::fmt::Display| cache_err(&format!("read {key}"), e);
    let builder = ParquetRecordBatchReaderBuilder::try_new(body).map_err(|e| ctx(&e))?;
    let selected: Vec<usize> = builder
        .metadata()
        .row_groups()
        .iter()
        .enumerate()
        .filter(|(_, rg)| row_group_may_match(rg, wanted, start_ns, end_ns))
        .map(|(i, _)| i)
        .collect();
    let total = builder.metadata().num_row_groups();
    stats.row_groups_read += selected.len();
    stats.row_groups_skipped += total - selected.len();
    if selected.is_empty() {
        return Ok(());
    }
    let reader = builder.with_row_groups(selected).build().map_err(|e| ctx(&e))?;
    for batch in reader {
        let batch = batch.map_err(|e| ctx(&e))?;
        let col =
            |name: &str| batch.column_by_name(name).ok_or_else(|| ctx(&format!("missing column {name}")));
        let ids = col("series_id")?
            .as_any()
            .downcast_ref::<StringArray>()
            .ok_or_else(|| ctx(&"series_id is not utf8"))?;
        let ts = col("ts")?
            .as_any()
            .downcast_ref::<TimestampNanosecondArray>()
            .ok_or_else(|| ctx(&"ts is not timestamp[ns]"))?;
        let values = col("value")?
            .as_any()
            .downcast_ref::<Float64Array>()
            .ok_or_else(|| ctx(&"value is not float64"))?;
        let quality = col("quality")?
            .as_any()
            .downcast_ref::<UInt8Array>()
            .ok_or_else(|| ctx(&"quality is not uint8"))?;
        let ingest = batch
            .column_by_name("ingest_ts")
            .and_then(|c| c.as_any().downcast_ref::<TimestampNanosecondArray>());
        for i in 0..batch.num_rows() {
            let t = ts.value(i);
            if t < start_ns || t >= end_ns {
                continue;
            }
            let Some(list) = rows.get_mut(ids.value(i)) else { continue };
            list.push(Row {
                ts: t,
                write_ns,
                value: if values.is_null(i) { f64::NAN } else { values.value(i) },
                quality: Quality::from_u8(quality.value(i)),
                ingest_ts: ingest.and_then(|a| (!a.is_null(i)).then(|| a.value(i))),
            });
        }
    }
    Ok(())
}

/// FNV-1a 64 of the series id, mod [`BUCKETS`].
pub fn bucket_of(series_id: &str) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in series_id.as_bytes() {
        h ^= u64::from(*b);
        h = h.wrapping_mul(0x0000_0100_0000_01b3);
    }
    h % BUCKETS
}

/// `write_ns` from a key `.../part-{write_ns:019}-{rand}.parquet`; 0 when absent.
fn write_ns_of(key: &str) -> i64 {
    key.rsplit('/')
        .next()
        .and_then(|name| name.strip_prefix("part-"))
        .and_then(|rest| rest.split('-').next())
        .and_then(|n| n.parse().ok())
        .unwrap_or(0)
}

static LAST_WRITE_NS: AtomicI64 = AtomicI64::new(0);

/// Wall-clock ns, strictly increasing within the process so two writes never tie.
fn next_write_ns() -> i64 {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos() as i64)
        .unwrap_or(0);
    let mut prev = LAST_WRITE_NS.load(Ordering::Relaxed);
    loop {
        let next = now.max(prev + 1);
        match LAST_WRITE_NS.compare_exchange_weak(prev, next, Ordering::Relaxed, Ordering::Relaxed) {
            Ok(_) => return next,
            Err(p) => prev = p,
        }
    }
}

static COUNTER: AtomicU64 = AtomicU64::new(0);

/// A random-enough suffix for object names (no crypto needed: names only have to be unique).
fn random_u64() -> u64 {
    use std::hash::{BuildHasher, Hasher};
    let mut h = std::collections::hash_map::RandomState::new().build_hasher();
    h.write_u64(COUNTER.fetch_add(1, Ordering::Relaxed));
    h.write_i64(next_write_ns());
    h.finish()
}

/// Days since 1970-01-01 to (year, month) in the proleptic Gregorian calendar (UTC).
fn civil_from_days(z: i64) -> (i64, u32) {
    // Howard Hinnant's algorithm.
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    let y = yoe + era * 400 + i64::from(m <= 2);
    (y, m)
}

/// Days since 1970-01-01 of the first day of (year, month); month may be 13 (next January).
fn days_from_civil(y: i64, m: u32) -> i64 {
    let (y, m) = if m > 12 { (y + 1, m - 12) } else { (y, m) };
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y.rem_euclid(400);
    let mp = if m > 2 { m - 3 } else { m + 9 } as i64;
    let doy = (153 * mp + 2) / 5;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

const NS_PER_DAY: i64 = 86_400_000_000_000;

fn year_month(ts_ns: i64) -> (i64, u32) {
    civil_from_days(ts_ns.div_euclid(NS_PER_DAY))
}

fn month_start_ns(y: i64, m: u32) -> i64 {
    days_from_civil(y, m).saturating_mul(NS_PER_DAY)
}

/// UTC months overlapping `[start_ns, end_ns)`, in order.
fn months_between(start_ns: i64, end_ns: i64) -> Vec<(i64, u32)> {
    let mut out = Vec::new();
    let (mut y, mut m) = year_month(start_ns);
    let last = year_month(end_ns - 1);
    while (y, m) <= last {
        out.push((y, m));
        if m == 12 {
            y += 1;
            m = 1;
        } else {
            m += 1;
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    const DAY: i64 = NS_PER_DAY;
    const HOUR: i64 = 3_600_000_000_000;

    fn temp_store() -> (tempdir::Dir, Cache) {
        let dir = tempdir::Dir::new();
        let cache = Cache::open(&StoreConfig { url: dir.path().to_string(), ..Default::default() }).unwrap();
        (dir, cache)
    }

    /// Hourly frame from `start` for `hours` hours, value = hour index + offset.
    fn hourly(id: &str, start: i64, hours: usize, offset: f64) -> SeriesFrame {
        let ts: Vec<i64> = (0..hours).map(|i| start + i as i64 * HOUR).collect();
        let values: Vec<f64> = (0..hours).map(|i| i as f64 + offset).collect();
        let quality: Vec<Quality> =
            (0..hours).map(|i| if i % 7 == 0 { Quality::Uncertain } else { Quality::Good }).collect();
        let ingest: Vec<i64> = ts.iter().map(|t| t + 5_000_000_000).collect();
        SeriesFrame::new(SeriesMeta::new(id), ts, values, quality).unwrap().with_ingest_ts(ingest).unwrap()
    }

    /// 2026-01-15 00:00 UTC.
    fn jan15() -> i64 {
        month_start_ns(2026, 1) + 14 * DAY
    }

    #[test]
    fn calendar_helpers_round_trip() {
        assert_eq!(year_month(0), (1970, 1));
        assert_eq!(month_start_ns(1970, 1), 0);
        assert_eq!(year_month(month_start_ns(2024, 3) - 1), (2024, 2)); // leap year February
        assert_eq!(year_month(month_start_ns(2026, 13)), (2027, 1));
        assert_eq!(months_between(month_start_ns(2025, 11) + DAY, month_start_ns(2026, 2) + DAY).len(), 4);
        assert_eq!(months_between(month_start_ns(2026, 1), month_start_ns(2026, 2)), vec![(2026, 1)]);
    }

    #[test]
    fn round_trip_local() {
        let (_dir, cache) = temp_store();
        let a = hourly("series-a", jan15(), 24 * 70, 0.0); // mid-January to late March
        let b = hourly("series-b", jan15(), 24 * 70, 100.0);
        let ra = cache.write("raw", "src", &a).unwrap();
        cache.write("raw", "src", &b).unwrap();
        assert_eq!(ra.rows, a.len());
        assert_eq!(ra.files.len(), 3, "three months, three files");
        assert_eq!((ra.start_ns, ra.end_ns), (a.ts[0], a.ts[a.len() - 1] + 1));

        let (s, e) = (month_start_ns(2026, 2) + 3 * DAY, month_start_ns(2026, 2) + 5 * DAY);
        let got = cache.read("raw", "src", &["series-a"], s, e).unwrap();
        assert_eq!(got.len(), 1);
        let f = &got[0];
        assert_eq!(f.len(), 48);
        assert_eq!(f.ts[0], s);
        assert!(f.ts.windows(2).all(|w| w[0] < w[1]));
        let i0 = a.ts.iter().position(|t| *t == s).unwrap();
        assert_eq!(f.values, a.values[i0..i0 + 48]);
        assert_eq!(f.quality, a.quality[i0..i0 + 48]);
        assert_eq!(f.ingest_ts.as_deref(), a.ingest_ts.as_deref().map(|v| &v[i0..i0 + 48]));
    }

    #[test]
    fn empty_and_unknown_series() {
        let (_dir, cache) = temp_store();
        let empty = SeriesFrame::with_default_quality(SeriesMeta::new("x"), vec![], vec![]).unwrap();
        assert_eq!(cache.write("raw", "src", &empty).unwrap().rows, 0);
        let got = cache.read("raw", "src", &["nothing-here"], 0, DAY).unwrap();
        assert!(got[0].is_empty());
        assert_eq!(got[0].meta.id, "nothing-here");
    }

    #[test]
    fn last_write_wins() {
        let (_dir, cache) = temp_store();
        cache.write("raw", "src", &hourly("s", jan15(), 48, 0.0)).unwrap();
        // Overwrite hours 10..20 with new values.
        let newer = hourly("s", jan15() + 10 * HOUR, 10, 1000.0);
        cache.write("raw", "src", &newer).unwrap();
        let f = &cache.read("raw", "src", &["s"], jan15(), jan15() + 48 * HOUR).unwrap()[0];
        assert_eq!(f.len(), 48);
        assert_eq!(f.values[9], 9.0);
        assert_eq!(f.values[10], 1000.0);
        assert_eq!(f.values[19], 1009.0);
        assert_eq!(f.values[20], 20.0);
    }

    #[test]
    fn prunes_buckets_and_months() {
        let (_dir, cache) = temp_store();
        let ids = ["p-1", "p-2", "p-3", "p-4", "p-5", "p-6"];
        for id in ids {
            cache.write("raw", "src", &hourly(id, jan15(), 24 * 70, 0.0)).unwrap();
        }
        let (s, e) = (month_start_ns(2026, 2), month_start_ns(2026, 2) + DAY);
        let (frames, stats) = cache.read_with_stats("raw", "src", &["p-3"], s, e).unwrap();
        assert_eq!(frames[0].len(), 24);
        assert_eq!(stats.prefixes_listed, 1, "one bucket × one month");
        let same_bucket = ids.iter().filter(|id| bucket_of(id) == bucket_of("p-3")).count();
        assert_eq!(stats.files_read, same_bucket, "only files of p-3's bucket and month");
    }

    #[test]
    fn row_groups_outside_the_window_are_skipped() {
        let (_dir, cache) = temp_store();
        cache.write("raw", "src", &hourly("s", jan15(), 24, 0.0)).unwrap();
        let (frames, stats) = cache
            .read_with_stats("raw", "src", &["s"], month_start_ns(2026, 1), month_start_ns(2026, 1) + DAY)
            .unwrap();
        assert!(frames[0].is_empty());
        assert_eq!((stats.files_read, stats.row_groups_skipped, stats.row_groups_read), (1, 1, 0));
    }

    #[test]
    fn rejects_bad_urls_and_segments() {
        let err = |cfg: StoreConfig| Cache::open(&cfg).unwrap_err().to_string();
        assert!(err(StoreConfig { url: "gs://bucket".into(), ..Default::default() }).contains("unsupported"));
        assert!(err(StoreConfig { url: "s3://bucket".into(), ..Default::default() }).contains("access key"));
        assert!(err(StoreConfig { url: "s3://".into(), ..Default::default() }).contains("bucket"));
        let (_dir, cache) = temp_store();
        let f = hourly("s", jan15(), 2, 0.0);
        assert!(cache.write("raw", "../etc", &f).is_err());
        assert!(cache.read("..", "src", &["s"], 0, 1).is_err());
    }

    #[test]
    fn reserved_characters_in_ids_round_trip() {
        // `Path::from` percent-encodes `#`, `%`, spaces and non-ASCII; the listed paths are
        // already encoded, so a read must use them as they are, not encode them again.
        let (_dir, cache) = temp_store();
        let f = hourly("tag #1 %Ä", jan15(), 24, 0.0);
        cache.write("raw", "plant#1", &f).unwrap();
        let got = cache.read("raw", "plant#1", &["tag #1 %Ä"], jan15(), jan15() + DAY).unwrap();
        assert_eq!(got[0].len(), 24);
    }

    #[test]
    fn write_ns_parsing_and_monotonic() {
        assert_eq!(write_ns_of("raw/src/0a/2026/01/part-0000000000000000042-00ff.parquet"), 42);
        assert_eq!(write_ns_of("garbage"), 0);
        let a = next_write_ns();
        let b = next_write_ns();
        assert!(b > a);
    }

    /// Round trip against a real S3 endpoint; runs when `TABAYYUN_TEST_S3_URL`
    /// (`s3://bucket`), `TABAYYUN_TEST_S3_ENDPOINT`, `TABAYYUN_TEST_S3_ACCESS_KEY_ID` and
    /// `TABAYYUN_TEST_S3_SECRET_ACCESS_KEY` are set (CI runs RustFS as a service).
    #[test]
    fn s3_round_trip() {
        let Ok(url) = std::env::var("TABAYYUN_TEST_S3_URL") else {
            eprintln!("skipped: TABAYYUN_TEST_S3_URL not set");
            return;
        };
        let env = |k: &str| std::env::var(k).ok();
        let run = format!("{url}/test-{:016x}", random_u64());
        let cfg = StoreConfig {
            url: run,
            s3_endpoint: env("TABAYYUN_TEST_S3_ENDPOINT"),
            s3_region: env("TABAYYUN_TEST_S3_REGION"),
            s3_access_key_id: env("TABAYYUN_TEST_S3_ACCESS_KEY_ID"),
            s3_secret_access_key: env("TABAYYUN_TEST_S3_SECRET_ACCESS_KEY"),
            s3_allow_http: env("TABAYYUN_TEST_S3_ENDPOINT").is_some_and(|e| e.starts_with("http://")),
        };
        let cache = Cache::open(&cfg).unwrap();
        let a = hourly("s3-a", jan15(), 24 * 40, 0.0);
        let r = cache.write("raw", "plant #1", &a).unwrap();
        assert_eq!(r.files.len(), 2);
        cache.write("raw", "plant #1", &hourly("s3-a", jan15() + 5 * HOUR, 2, 500.0)).unwrap();
        let f = &cache.read("raw", "plant #1", &["s3-a"], jan15(), jan15() + DAY).unwrap()[0];
        assert_eq!(f.len(), 24);
        assert_eq!((f.values[4], f.values[5], f.values[6], f.values[7]), (4.0, 500.0, 501.0, 7.0));
        // Hours 5 and 6 come from the newer write (its first sample is marked uncertain).
        assert_eq!(f.quality[..5], a.quality[..5]);
        assert_eq!((f.quality[5], f.quality[6]), (Quality::Uncertain, Quality::Good));
        assert_eq!(f.quality[7..], a.quality[7..24]);
    }

    /// Minimal self-deleting temp directory (no extra dev-dependency).
    mod tempdir {
        pub struct Dir(std::path::PathBuf);
        impl Dir {
            pub fn new() -> Self {
                let p =
                    std::env::temp_dir().join(format!("tabayyun-cache-{:016x}", super::super::random_u64()));
                std::fs::create_dir_all(&p).unwrap();
                Dir(p)
            }
            pub fn path(&self) -> &str {
                self.0.to_str().unwrap()
            }
        }
        impl Drop for Dir {
            fn drop(&mut self) {
                let _ = std::fs::remove_dir_all(&self.0);
            }
        }
    }
}
