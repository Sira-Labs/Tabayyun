//! Epoch-integer timestamps in the CLI follow ADR-0014 as the API does (spec 017).

use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::Arc;

const T0: i64 = 1_700_000_000 / 3600 * 3600;

fn scratch(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("tabayyun-cli-ts-{}-{name}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// 48 hourly rows with the given timestamp cells.
fn csv(dir: &Path, name: &str, ts: impl Fn(i64) -> String) -> PathBuf {
    let rows: Vec<String> = std::iter::once("ts,value".to_string())
        .chain((0..48).map(|i| format!("{},{}", ts(i), 20 + i % 5)))
        .collect();
    let path = dir.join(name);
    std::fs::write(&path, rows.join("\n") + "\n").unwrap();
    path
}

fn run(path: &Path, extra: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_tabayyun"))
        .args(["run", "--input", path.to_str().unwrap()])
        .args(extra)
        .output()
        .unwrap()
}

fn stderr(out: &Output) -> String {
    String::from_utf8_lossy(&out.stderr).into_owned()
}

#[test]
fn column_unit_is_uniform() {
    // Epoch seconds with one stray millisecond value: per-cell guessing read it as a valid
    // millisecond instant; the column is seconds, so it is year ~55,800 and the run fails.
    let dir = scratch("uniform");
    let path = csv(&dir, "stray.csv", |i| {
        let t = T0 + i * 3600;
        if i == 17 {
            (t * 1000).to_string()
        } else {
            t.to_string()
        }
    });
    let out = run(&path, &[]);
    assert_eq!(out.status.code(), Some(2), "{}", stderr(&out));
    let err = stderr(&out);
    assert!(err.contains("column `ts` row 18"), "{err}");
    assert!(err.contains("read as seconds"), "{err}");
    assert!(err.contains("--ts-unit"), "{err}");
}

#[test]
fn wrong_unit_exits_2() {
    let dir = scratch("wrong");
    let path = csv(&dir, "seconds.csv", |i| (T0 + i * 3600).to_string());
    let out = run(&path, &["--ts-unit", "ms"]);
    assert_eq!(out.status.code(), Some(2), "{}", stderr(&out));
    assert!(stderr(&out)
        .contains("read as milliseconds is not between 1971-01-01 and 2199-12-31 (unit declared)"));
    // Declared correctly, the same file runs and says how it was read.
    let out = run(&path, &["--ts-unit", "s"]);
    assert!(out.status.success(), "{}", stderr(&out));
    let json: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(json["ts_unit"], "s");
    assert_eq!(json["window"]["start"], T0 * 1_000_000_000);
}

#[test]
fn ts_unit_in_output() {
    let dir = scratch("output");
    for (name, cells, unit) in [
        ("s.csv", 1i64, "s"),
        ("ms.csv", 1_000, "ms"),
        ("us.csv", 1_000_000, "us"),
        ("ns.csv", 1_000_000_000, "ns"),
    ] {
        let path = csv(&dir, name, |i| ((T0 + i * 3600) * cells).to_string());
        let out = run(&path, &[]);
        assert!(out.status.success(), "{name}: {}", stderr(&out));
        let json: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
        assert_eq!(json["ts_unit"], unit, "{name}");
        assert_eq!(json["window"]["start"], T0 * 1_000_000_000, "{name}");
    }
    let path =
        csv(&dir, "text.csv", |i| chrono::DateTime::from_timestamp(T0 + i * 3600, 0).unwrap().to_rfc3339());
    let json: serde_json::Value = serde_json::from_slice(&run(&path, &[]).stdout).unwrap();
    assert_eq!(json["ts_unit"], "text");
    // check-multi reports it too.
    let out = Command::new(env!("CARGO_BIN_EXE_tabayyun"))
        .args(["check-multi", dir.join("ms.csv").to_str().unwrap(), "--value-cols", "value"])
        .output()
        .unwrap();
    assert!(out.status.success(), "{}", stderr(&out));
    let json: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(json["ts_unit"], "ms");
}

#[test]
fn mixed_text_and_integers_exit_2() {
    let dir = scratch("mixed");
    let path = csv(&dir, "mixed.csv", |i| {
        let t = T0 + i * 3600;
        if i == 5 {
            chrono::DateTime::from_timestamp(t, 0).unwrap().to_rfc3339()
        } else {
            t.to_string()
        }
    });
    let out = run(&path, &[]);
    assert_eq!(out.status.code(), Some(2), "{}", stderr(&out));
    assert!(stderr(&out).contains("mixes epoch integers and text: row 6 is text"), "{}", stderr(&out));
}

#[test]
fn parquet_integer_seconds() {
    use arrow::array::{Float64Array, Int64Array, RecordBatch};
    use arrow::datatypes::{DataType, Field, Schema};
    let dir = scratch("parquet");
    let path = dir.join("seconds.parquet");
    let schema = Arc::new(Schema::new(vec![
        Field::new("ts", DataType::Int64, false),
        Field::new("value", DataType::Float64, true),
    ]));
    let batch = RecordBatch::try_new(
        schema.clone(),
        vec![
            Arc::new(Int64Array::from_iter_values((0..48).map(|i| T0 + i * 3600))),
            Arc::new(Float64Array::from_iter_values((0..48).map(|i| 20.0 + (i % 5) as f64))),
        ],
    )
    .unwrap();
    let mut w =
        parquet::arrow::ArrowWriter::try_new(std::fs::File::create(&path).unwrap(), schema, None).unwrap();
    w.write(&batch).unwrap();
    w.close().unwrap();
    // Before spec 017 these integers were taken as nanoseconds: 1970.
    let out = run(&path, &[]);
    assert!(out.status.success(), "{}", stderr(&out));
    let json: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(json["ts_unit"], "s");
    assert_eq!(json["window"]["start"], T0 * 1_000_000_000);
    let out = run(&path, &["--ts-unit", "ns"]);
    assert_eq!(out.status.code(), Some(2), "{}", stderr(&out));
}
