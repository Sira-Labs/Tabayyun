//! `tabayyun` CLI: run the built-in checks on a CSV or Parquet file (one series, or one per
//! column with cross-series checks over groups), or generate synthetic test data. Output is
//! JSON so it can feed the API, notebooks or CI.

use chrono::{DateTime, Utc};
use clap::{Args, Parser, Subcommand};
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use std::collections::BTreeMap;
use std::fs::File;
use std::path::PathBuf;
use tabayyun_core::cache::{Cache, StoreConfig};
use tabayyun_core::synth::{self, inject, Rng, SynthSpec};
use tabayyun_core::time::NS_PER_SEC;
use tabayyun_core::{
    CheckConfig, CheckContext, CheckOutput, Profile, Quality, Registry, Scorer, SeriesFrame, SeriesGroup,
    SeriesMeta, Window,
};

#[derive(Parser)]
#[command(name = "tabayyun", version, about = "Tabayyun time-series data-quality core")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Run checks on a file and print findings and scores as JSON.
    Run(RunArgs),
    /// Run checks on a wide file (one series per value column) and cross-series checks on
    /// groups; prints one report per series and the groups that could not run (spec 008).
    CheckMulti(CheckMultiArgs),
    /// Generate a synthetic series (CSV) with optional injected faults.
    Synth(SynthArgs),
    /// List built-in checks.
    Checks,
    /// Write, read or benchmark the Parquet cache (spec 006).
    #[command(subcommand)]
    Cache(CacheCmd),
}

#[derive(Subcommand)]
enum CacheCmd {
    /// Write one series from a file to the cache.
    Write {
        #[command(flatten)]
        store: StoreArgs,
        #[command(flatten)]
        src: InputArgs,
        /// Source id (first path segment under the layer).
        #[arg(long, default_value = "cli")]
        source: String,
        #[arg(long, default_value = "raw")]
        layer: String,
    },
    /// Read series over a time range and print a JSON summary per series.
    Read {
        #[command(flatten)]
        store: StoreArgs,
        #[arg(long, default_value = "cli")]
        source: String,
        #[arg(long, default_value = "raw")]
        layer: String,
        /// Comma-separated series ids.
        #[arg(long)]
        series: String,
        /// Range start (RFC 3339).
        #[arg(long)]
        start: String,
        /// Range end, exclusive (RFC 3339).
        #[arg(long)]
        end: String,
    },
    /// Write and read synthetic rows and print throughput.
    Bench {
        #[command(flatten)]
        store: StoreArgs,
        #[arg(long, default_value_t = 10_000_000)]
        rows: usize,
        /// Series the rows are spread over.
        #[arg(long, default_value_t = 1)]
        series: usize,
        #[arg(long, default_value = "1s")]
        interval: String,
    },
}

/// Cache location; S3 options also come from the `TABAYYUN_S3_*` environment.
#[derive(Args)]
struct StoreArgs {
    /// Local path, file:// or s3://bucket[/prefix].
    #[arg(long, env = "TABAYYUN_CACHE_URL")]
    store: String,
    #[arg(long, env = "TABAYYUN_S3_ENDPOINT")]
    s3_endpoint: Option<String>,
    #[arg(long, env = "TABAYYUN_S3_REGION")]
    s3_region: Option<String>,
    #[arg(long, env = "TABAYYUN_S3_ACCESS_KEY_ID")]
    s3_access_key_id: Option<String>,
    /// Secret key; taken from the environment only, so it never shows in a process list.
    #[arg(skip = std::env::var("TABAYYUN_S3_SECRET_ACCESS_KEY").ok())]
    s3_secret_access_key: Option<String>,
    #[arg(long, env = "TABAYYUN_S3_ALLOW_HTTP")]
    s3_allow_http: bool,
}

impl StoreArgs {
    fn open(&self) -> Result<Cache, Box<dyn std::error::Error>> {
        Ok(Cache::open(&StoreConfig {
            url: self.store.clone(),
            s3_endpoint: self.s3_endpoint.clone(),
            s3_region: self.s3_region.clone(),
            s3_access_key_id: self.s3_access_key_id.clone(),
            s3_secret_access_key: self.s3_secret_access_key.clone(),
            s3_allow_http: self.s3_allow_http,
        })?)
    }
}

/// Where a series comes from: a CSV or Parquet file and its column names.
#[derive(Args)]
struct InputArgs {
    /// Input file (.csv or .parquet).
    #[arg(long)]
    input: PathBuf,
    #[arg(long, default_value = "ts")]
    ts_col: String,
    #[arg(long, default_value = "value")]
    value_col: String,
    #[arg(long)]
    quality_col: Option<String>,
    /// Column with the time each sample arrived (enables tby.latency).
    #[arg(long)]
    ingest_col: Option<String>,
    #[arg(long, default_value = "series")]
    series_id: String,
}

#[derive(Args)]
struct RunArgs {
    #[command(flatten)]
    src: InputArgs,
    /// Engineering unit (enables unit-based limits, e.g. "%", "m3/h").
    #[arg(long)]
    unit: Option<String>,
    #[arg(long, allow_hyphen_values = true)]
    physical_min: Option<f64>,
    #[arg(long, allow_hyphen_values = true)]
    physical_max: Option<f64>,
    /// Expected sampling interval, e.g. "1m", "15m". Default: derived from data.
    #[arg(long)]
    interval: Option<String>,
    /// "Now" for staleness (RFC 3339). Default: last timestamp in the file.
    #[arg(long)]
    now: Option<String>,
    /// JSON file with a list of {"id": ..., "params": {...}} check configs.
    #[arg(long)]
    config: Option<PathBuf>,
    /// Compute the baseline profile on the file itself and use it for adaptive thresholds.
    #[arg(long, default_value_t = true)]
    profile: bool,
    /// Pretty-print JSON.
    #[arg(long)]
    pretty: bool,
}

#[derive(Args)]
struct CheckMultiArgs {
    /// Wide input file (.csv or .parquet): one timestamp column and one column per series.
    file: PathBuf,
    /// Comma-separated value columns; each becomes a series named after its column.
    #[arg(long, value_delimiter = ',', required = true)]
    value_cols: Vec<String>,
    #[arg(long, default_value = "ts")]
    ts_col: String,
    /// JSON file with a list of series groups ({"id", "name", "kind", "members", "params"}).
    #[arg(long)]
    groups: Option<PathBuf>,
    /// JSON file mapping series id (the column name) to metadata (unit, physical_min, ...).
    #[arg(long)]
    metas: Option<PathBuf>,
    /// "Now" for staleness (RFC 3339). Default: last timestamp in the file.
    #[arg(long)]
    now: Option<String>,
    /// JSON file with a list of {"id": ..., "params": {...}} check configs.
    #[arg(long)]
    config: Option<PathBuf>,
    /// Compute each series' baseline profile on the file itself for adaptive thresholds.
    #[arg(long, default_value_t = true)]
    profile: bool,
    /// Pretty-print JSON.
    #[arg(long)]
    pretty: bool,
}

#[derive(Args)]
struct SynthArgs {
    #[arg(long)]
    out: PathBuf,
    #[arg(long, default_value_t = 1440)]
    n: usize,
    #[arg(long, default_value = "1m")]
    interval: String,
    #[arg(long, default_value_t = 42)]
    seed: u64,
    /// Comma-separated faults: gap,flatline,spikes,nans,negative,range,duplicates,out_of_order,jitter
    #[arg(long, default_value = "")]
    faults: String,
}

fn main() {
    if let Err(e) = real_main() {
        eprintln!("error: {e}");
        std::process::exit(1);
    }
}

fn real_main() -> Result<(), Box<dyn std::error::Error>> {
    match Cli::parse().cmd {
        Cmd::Checks => {
            for id in Registry::builtin_ids() {
                println!("{id}");
            }
            Ok(())
        }
        Cmd::Synth(a) => synth_cmd(a),
        Cmd::Run(a) => run_cmd(a),
        Cmd::CheckMulti(a) => check_multi_cmd(a),
        Cmd::Cache(c) => cache_cmd(c),
    }
}

fn rfc3339_ns(s: &str) -> Result<i64, Box<dyn std::error::Error>> {
    let t: DateTime<Utc> = s.parse()?;
    Ok(t.timestamp_nanos_opt().ok_or("timestamp out of range")?)
}

fn cache_cmd(c: CacheCmd) -> Result<(), Box<dyn std::error::Error>> {
    match c {
        CacheCmd::Write { store, src, source, layer } => {
            let frame = load(&src, SeriesMeta::new(&src.series_id))?;
            let report = store.open()?.write(&layer, &source, &frame)?;
            println!("{}", serde_json::to_string(&report)?);
        }
        CacheCmd::Read { store, source, layer, series, start, end } => {
            let ids: Vec<&str> = series.split(',').map(str::trim).filter(|s| !s.is_empty()).collect();
            let (frames, stats) = store.open()?.read_with_stats(
                &layer,
                &source,
                &ids,
                rfc3339_ns(&start)?,
                rfc3339_ns(&end)?,
            )?;
            let series: Vec<_> = frames
                .iter()
                .map(|f| {
                    serde_json::json!({"series_id": f.meta.id, "rows": f.len(),
                        "first_ts": f.first_ts(), "last_ts": f.last_ts()})
                })
                .collect();
            println!("{}", serde_json::json!({"series": series, "stats": stats}));
        }
        CacheCmd::Bench { store, rows, series, interval } => {
            let interval_ns = tabayyun_core::time::parse_duration(&interval).ok_or("bad --interval")?;
            let cache = store.open()?;
            let per = rows.div_ceil(series.max(1));
            let start = 1_767_225_600 * NS_PER_SEC; // 2026-01-01T00:00:00Z
            let mut written = 0usize;
            let mut files = 0usize;
            let ids: Vec<String> = (0..series.max(1)).map(|i| format!("bench-{i:04}")).collect();
            // Generate first so the timings measure the cache, not the synthesiser.
            let frames: Vec<SeriesFrame> = ids
                .iter()
                .map(|id| {
                    synth::generate(&SynthSpec {
                        series_id: id.clone(),
                        n: per,
                        interval_ns,
                        start_ns: start,
                        ..SynthSpec::default()
                    })
                })
                .collect();
            let t0 = std::time::Instant::now();
            for f in &frames {
                let r = cache.write("raw", "bench", f)?;
                written += r.rows;
                files += r.files.len();
            }
            let write_s = t0.elapsed().as_secs_f64();
            let end = start + per as i64 * interval_ns;
            let refs: Vec<&str> = ids.iter().map(String::as_str).collect();
            let t1 = std::time::Instant::now();
            let (_, stats) = cache.read_with_stats("raw", "bench", &refs, start, end)?;
            let read_s = t1.elapsed().as_secs_f64();
            println!(
                "{}",
                serde_json::json!({
                    "rows": written, "series": ids.len(), "files": files,
                    "write_s": write_s, "write_rows_per_s": written as f64 / write_s,
                    "read_s": read_s, "read_rows_per_s": stats.rows as f64 / read_s, "read": stats,
                })
            );
        }
    }
    Ok(())
}

fn run_cmd(a: RunArgs) -> Result<(), Box<dyn std::error::Error>> {
    let mut meta = SeriesMeta::new(&a.src.series_id);
    meta.unit = a.unit.clone();
    meta.physical_min = a.physical_min;
    meta.physical_max = a.physical_max;
    if let Some(i) = &a.interval {
        meta.expected_interval_ns = Some(tabayyun_core::time::parse_duration(i).ok_or("bad --interval")?);
    }
    let frame = load(&a.src, meta)?;
    let mut ctx = CheckContext::from_frame(&frame);
    if let Some(now) = &a.now {
        let t: DateTime<Utc> = now.parse()?;
        ctx.now_ns = t.timestamp_nanos_opt().ok_or("now out of range")?;
        ctx.window = Window::new(ctx.window.start, ctx.window.end.max(ctx.now_ns));
    }
    let profile = if a.profile { Some(Profile::compute(&frame)) } else { None };
    if let Some(p) = &profile {
        ctx = ctx.with_profile(p.clone());
    }
    let configs: Vec<CheckConfig> = match &a.config {
        Some(p) => serde_json::from_reader(File::open(p)?)?,
        None => Registry::default_configs(),
    };
    let out = Registry::run(&configs, &frame, &ctx)?;
    print_json(&report_json(&frame, &ctx, profile.as_ref(), out), a.pretty)
}

fn check_multi_cmd(a: CheckMultiArgs) -> Result<(), Box<dyn std::error::Error>> {
    let mut metas: BTreeMap<String, SeriesMeta> = match &a.metas {
        Some(p) => serde_json::from_reader(File::open(p)?)?,
        None => BTreeMap::new(),
    };
    let groups: Vec<SeriesGroup> = match &a.groups {
        Some(p) => serde_json::from_reader(File::open(p)?)?,
        None => Vec::new(),
    };
    let configs: Vec<CheckConfig> = match &a.config {
        Some(p) => serde_json::from_reader(File::open(p)?)?,
        None => Registry::default_multi_configs(),
    };
    let mut frames = Vec::with_capacity(a.value_cols.len());
    for col in &a.value_cols {
        let meta = metas.remove(col).unwrap_or_else(|| SeriesMeta::new(col));
        if meta.id != *col {
            return Err(format!("metadata for column {col} names series {}", meta.id).into());
        }
        let src = InputArgs {
            input: a.file.clone(),
            ts_col: a.ts_col.clone(),
            value_col: col.clone(),
            quality_col: None,
            ingest_col: None,
            series_id: col.clone(),
        };
        frames.push(load(&src, meta)?);
    }
    let start = frames.iter().filter_map(SeriesFrame::first_ts).min().unwrap_or(0);
    let end = frames.iter().filter_map(SeriesFrame::last_ts).max().map_or(start, |t| t + 1);
    let mut ctx = CheckContext { now_ns: end - 1, window: Window::new(start, end), profile: None };
    if let Some(now) = &a.now {
        ctx.now_ns = rfc3339_ns(now)?;
        ctx.window = Window::new(start, end.max(ctx.now_ns));
    }
    let profiles: BTreeMap<String, Profile> = if a.profile {
        frames.iter().map(|f| (f.meta.id.clone(), Profile::compute(f))).collect()
    } else {
        BTreeMap::new()
    };
    let mut out = Registry::run_multi(&configs, &frames, &profiles, &groups, &ctx)?;
    let reports: serde_json::Map<String, serde_json::Value> = frames
        .iter()
        .map(|f| {
            let output = out.per_series.remove(&f.meta.id).unwrap_or_default();
            (f.meta.id.clone(), report_json(f, &ctx, profiles.get(&f.meta.id), output))
        })
        .collect();
    print_json(&serde_json::json!({"reports": reports, "groups_skipped": out.groups_skipped}), a.pretty)
}

/// One series' report: findings, metrics, score, profile and skipped checks.
fn report_json(
    frame: &SeriesFrame,
    ctx: &CheckContext,
    profile: Option<&Profile>,
    out: CheckOutput,
) -> serde_json::Value {
    let score = Scorer::default().score_window(&frame.meta.id, &out.findings, ctx.window);
    serde_json::json!({
        "series_id": frame.meta.id,
        "n_samples": frame.len(),
        "window": ctx.window,
        "now_ns": ctx.now_ns,
        "profile": profile,
        "score": score,
        "findings": out.findings,
        "metrics": out.metrics,
        "skipped": out.skipped.iter().map(|(c, f)| serde_json::json!({"check_id": c, "missing": f})).collect::<Vec<_>>(),
    })
}

fn print_json(value: &serde_json::Value, pretty: bool) -> Result<(), Box<dyn std::error::Error>> {
    let s = if pretty { serde_json::to_string_pretty(value)? } else { serde_json::to_string(value)? };
    println!("{s}");
    Ok(())
}

fn load(a: &InputArgs, meta: SeriesMeta) -> Result<SeriesFrame, Box<dyn std::error::Error>> {
    let ext = a.input.extension().and_then(|e| e.to_str()).unwrap_or("").to_ascii_lowercase();
    match ext.as_str() {
        "parquet" | "pq" => {
            let file = File::open(&a.input)?;
            let reader = ParquetRecordBatchReaderBuilder::try_new(file)?.build()?;
            let mut ts = Vec::new();
            let mut values = Vec::new();
            let mut quality = Vec::new();
            let mut ingest: Vec<i64> = Vec::new();
            for batch in reader {
                let b = batch?;
                let f = SeriesFrame::from_record_batch_ext(
                    meta.clone(),
                    &b,
                    &a.ts_col,
                    &a.value_col,
                    a.quality_col.as_deref(),
                    a.ingest_col.as_deref(),
                )?;
                ts.extend(f.ts);
                values.extend(f.values);
                quality.extend(f.quality);
                if let Some(i) = f.ingest_ts {
                    ingest.extend(i);
                }
            }
            let frame = SeriesFrame::new(meta, ts, values, quality)?;
            Ok(if a.ingest_col.is_some() { frame.with_ingest_ts(ingest)? } else { frame })
        }
        _ => load_csv(a, meta),
    }
}

fn load_csv(a: &InputArgs, meta: SeriesMeta) -> Result<SeriesFrame, Box<dyn std::error::Error>> {
    let mut rdr = csv::ReaderBuilder::new().flexible(true).from_path(&a.input)?;
    let headers = rdr.headers()?.clone();
    let idx = |name: &str| {
        headers.iter().position(|h| h == name).ok_or_else(|| format!("column `{name}` not found"))
    };
    let ti = idx(&a.ts_col)?;
    let vi = idx(&a.value_col)?;
    let qi = a.quality_col.as_deref().map(idx).transpose()?;
    let ii = a.ingest_col.as_deref().map(idx).transpose()?;
    let mut ts = Vec::new();
    let mut values = Vec::new();
    let mut quality = Vec::new();
    let mut ingest = Vec::new();
    for rec in rdr.records() {
        let rec = rec?;
        ts.push(parse_ts(rec.get(ti).unwrap_or(""))?);
        values.push(rec.get(vi).map(|s| s.trim().parse::<f64>().unwrap_or(f64::NAN)).unwrap_or(f64::NAN));
        quality.push(qi.and_then(|i| rec.get(i)).and_then(Quality::parse).unwrap_or(Quality::Good));
        if let Some(i) = ii {
            ingest.push(parse_ts(rec.get(i).unwrap_or(""))?);
        }
    }
    let frame = SeriesFrame::new(meta, ts, values, quality)?;
    Ok(if ii.is_some() { frame.with_ingest_ts(ingest)? } else { frame })
}

/// Parse one timestamp cell into ns since the Unix epoch: RFC 3339, epoch integers (unit
/// inferred from magnitude) or naive `YYYY-MM-DD[ T]HH:MM:SS[.fraction]` taken as UTC.
fn parse_ts(s: &str) -> Result<i64, Box<dyn std::error::Error>> {
    let s = s.trim();
    if let Ok(t) = s.parse::<DateTime<Utc>>() {
        return Ok(t.timestamp_nanos_opt().ok_or("timestamp out of range")?);
    }
    if let Ok(n) = s.parse::<i64>() {
        // Heuristic on magnitude: seconds, milliseconds, microseconds or nanoseconds.
        return Ok(match n.abs() {
            x if x < 100_000_000_000 => n * NS_PER_SEC,
            x if x < 100_000_000_000_000 => n * 1_000_000,
            x if x < 100_000_000_000_000_000 => n * 1_000,
            _ => n,
        });
    }
    // Naive timestamps (historian exports: PI, OPC, Petrobras 3W) are taken as UTC. `%.f`
    // also matches an absent fraction, so one pattern per separator covers both.
    for fmt in ["%Y-%m-%d %H:%M:%S%.f", "%Y-%m-%dT%H:%M:%S%.f"] {
        if let Ok(t) = chrono::NaiveDateTime::parse_from_str(s, fmt) {
            return Ok(t.and_utc().timestamp_nanos_opt().ok_or("timestamp out of range")?);
        }
    }
    Err(format!("cannot parse timestamp `{s}`").into())
}

fn synth_cmd(a: SynthArgs) -> Result<(), Box<dyn std::error::Error>> {
    let interval_ns = tabayyun_core::time::parse_duration(&a.interval).ok_or("bad --interval")?;
    let spec = SynthSpec { n: a.n, interval_ns, seed: a.seed, ..SynthSpec::default() };
    let mut f = synth::generate(&spec);
    let mut rng = Rng::new(a.seed ^ 0xABCD);
    let n = f.len();
    for fault in a.faults.split(',').map(str::trim).filter(|s| !s.is_empty()) {
        match fault {
            "gap" => {
                inject::gap(&mut f, n / 4, n / 20);
            }
            "flatline" => {
                inject::flatline(&mut f, n / 2, n / 10);
            }
            "spikes" => {
                inject::spikes(&mut f, &mut rng, 5, 8.0 * spec.amplitude);
            }
            "nans" => {
                inject::nans(&mut f, (3 * n) / 4, n / 40);
            }
            "negative" => {
                inject::set(&mut f, n / 8, 3, -1.0);
            }
            "range" => {
                inject::set(&mut f, (5 * n) / 8, 2, 1e6);
            }
            "duplicates" => {
                inject::duplicate(&mut f, n / 3, 2);
                inject::conflicting_duplicate(&mut f, n / 3 + 1, 1.0);
            }
            "out_of_order" => {
                inject::swap(&mut f, n / 5, n / 5 + 1);
            }
            "jitter" => {
                for t in f.ts.iter_mut() {
                    *t += (rng.uniform() * 0.4 * interval_ns as f64) as i64;
                }
            }
            other => return Err(format!("unknown fault `{other}`").into()),
        }
    }
    let mut w = csv::Writer::from_path(&a.out)?;
    w.write_record(["ts", "value", "quality"])?;
    for i in 0..f.len() {
        let t = DateTime::<Utc>::from_timestamp_nanos(f.ts[i]);
        let v = if f.values[i].is_nan() { String::new() } else { format!("{:.4}", f.values[i]) };
        w.write_record([t.to_rfc3339(), v, format!("{}", f.quality[i].as_u8())])?;
    }
    w.flush()?;
    eprintln!("wrote {} samples to {}", f.len(), a.out.display());
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::parse_ts;

    /// Historian exports mix separators, fractions, offsets and epoch integers.
    #[test]
    fn parses_historian_export_timestamps() {
        let base = parse_ts("2017-02-01T01:02:07Z").unwrap();
        assert_eq!(parse_ts("2017-02-01 01:02:07").unwrap(), base);
        assert_eq!(parse_ts("2017-02-01 01:02:07.000000000").unwrap(), base);
        assert_eq!(parse_ts("2017-02-01T01:02:07.5").unwrap(), base + 500_000_000);
        assert_eq!(parse_ts("2017-02-01T02:02:07+01:00").unwrap(), base);
        assert_eq!(parse_ts("1485910927").unwrap(), base);
        assert!(parse_ts("01/02/2017 01:02").is_err());
    }
}
