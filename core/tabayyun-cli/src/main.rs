//! `tabayyun` CLI: run the built-in checks on a CSV or Parquet file, or generate synthetic
//! test data. Output is JSON so it can feed the API, notebooks or CI.

use chrono::{DateTime, Utc};
use clap::{Args, Parser, Subcommand};
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use std::fs::File;
use std::path::PathBuf;
use tabayyun_core::synth::{self, inject, Rng, SynthSpec};
use tabayyun_core::time::NS_PER_SEC;
use tabayyun_core::{
    CheckConfig, CheckContext, Profile, Quality, Registry, Scorer, SeriesFrame, SeriesMeta, Window,
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
    /// Generate a synthetic series (CSV) with optional injected faults.
    Synth(SynthArgs),
    /// List built-in checks.
    Checks,
}

#[derive(Args)]
struct RunArgs {
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
    }
}

fn run_cmd(a: RunArgs) -> Result<(), Box<dyn std::error::Error>> {
    let mut meta = SeriesMeta::new(&a.series_id);
    meta.unit = a.unit.clone();
    meta.physical_min = a.physical_min;
    meta.physical_max = a.physical_max;
    if let Some(i) = &a.interval {
        meta.expected_interval_ns = Some(tabayyun_core::time::parse_duration(i).ok_or("bad --interval")?);
    }
    let frame = load(&a, meta)?;
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
    let score = Scorer::default().score_window(&frame.meta.id, &out.findings, ctx.window);
    let report = serde_json::json!({
        "series_id": frame.meta.id,
        "n_samples": frame.len(),
        "window": ctx.window,
        "now_ns": ctx.now_ns,
        "profile": profile,
        "score": score,
        "findings": out.findings,
        "metrics": out.metrics,
        "skipped": out.skipped.iter().map(|(c, f)| serde_json::json!({"check_id": c, "missing": f})).collect::<Vec<_>>(),
    });
    let s = if a.pretty { serde_json::to_string_pretty(&report)? } else { serde_json::to_string(&report)? };
    println!("{s}");
    Ok(())
}

fn load(a: &RunArgs, meta: SeriesMeta) -> Result<SeriesFrame, Box<dyn std::error::Error>> {
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

fn load_csv(a: &RunArgs, meta: SeriesMeta) -> Result<SeriesFrame, Box<dyn std::error::Error>> {
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
