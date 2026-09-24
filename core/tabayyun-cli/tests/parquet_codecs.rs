//! The CLI reads Parquet in every codec pyarrow writes (the 3W dataset ships brotli).

use std::path::PathBuf;
use std::process::Command;
use std::sync::Arc;

use arrow::array::{Float64Array, Int64Array, RecordBatch};
use arrow::datatypes::{DataType, Field, Schema};
use parquet::basic::{BrotliLevel, Compression, GzipLevel, ZstdLevel};
use parquet::file::properties::WriterProperties;

const T0: i64 = 1_700_000_000 / 3600 * 3600;

fn write(dir: &std::path::Path, name: &str, codec: Compression) -> PathBuf {
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
    let path = dir.join(name);
    let props = WriterProperties::builder().set_compression(codec).build();
    let mut w =
        parquet::arrow::ArrowWriter::try_new(std::fs::File::create(&path).unwrap(), schema, Some(props))
            .unwrap();
    w.write(&batch).unwrap();
    w.close().unwrap();
    path
}

#[test]
fn reads_every_pyarrow_codec() {
    let dir = std::env::temp_dir().join(format!("tabayyun-cli-codecs-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    for (name, codec) in [
        ("brotli", Compression::BROTLI(BrotliLevel::default())),
        ("gzip", Compression::GZIP(GzipLevel::default())),
        ("lz4", Compression::LZ4),
        ("lz4_raw", Compression::LZ4_RAW),
        ("snappy", Compression::SNAPPY),
        ("zstd", Compression::ZSTD(ZstdLevel::default())),
        ("none", Compression::UNCOMPRESSED),
    ] {
        let path = write(&dir, &format!("{name}.parquet"), codec);
        let out = Command::new(env!("CARGO_BIN_EXE_tabayyun"))
            .args(["run", "--input", path.to_str().unwrap()])
            .output()
            .unwrap();
        assert!(out.status.success(), "{name}: {}", String::from_utf8_lossy(&out.stderr));
        let json: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
        assert_eq!(json["window"]["start"], T0 * 1_000_000_000, "{name}");
    }
}
