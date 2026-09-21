//! Python bindings. Input frames arrive through the Arrow PyCapsule interface (anything with
//! `__arrow_c_stream__` or `__arrow_c_array__`: pyarrow tables/batches, polars frames, ...).
//! Results are returned as JSON strings and turned into dicts by the thin Python wrapper,
//! which keeps the binding free of any Python-object marshalling code.

use arrow::array::RecordBatch;
use arrow::compute::concat_batches;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3_arrow::{PyRecordBatch, PyRecordBatchReader};
use tabayyun_core::downsample::m4;
use tabayyun_core::synth::{generate, inject, Rng, SynthSpec};
use tabayyun_core::{CheckConfig, CheckContext, Profile, Registry, Scorer, SeriesFrame, SeriesMeta, Window};

fn err<E: std::fmt::Display>(e: E) -> PyErr {
    PyValueError::new_err(e.to_string())
}

fn batch_from_py(data: &Bound<'_, PyAny>) -> PyResult<RecordBatch> {
    if let Ok(rb) = data.extract::<PyRecordBatch>() {
        return Ok(rb.into_inner());
    }
    let reader = data.extract::<PyRecordBatchReader>()?.into_reader()?;
    let schema = reader.schema();
    let batches: Vec<RecordBatch> = reader.collect::<Result<_, _>>().map_err(err)?;
    concat_batches(&schema, &batches).map_err(err)
}

fn frame_from_py(
    data: &Bound<'_, PyAny>,
    meta_json: &str,
    ts_col: &str,
    value_col: &str,
    quality_col: Option<&str>,
    ingest_col: Option<&str>,
) -> PyResult<SeriesFrame> {
    let meta: SeriesMeta = serde_json::from_str(meta_json).map_err(err)?;
    let batch = batch_from_py(data)?;
    SeriesFrame::from_record_batch_ext(meta, &batch, ts_col, value_col, quality_col, ingest_col).map_err(err)
}

/// Run checks on an Arrow batch/stream. Returns a JSON report (findings, metrics, score, profile).
#[pyfunction]
#[pyo3(signature = (data, meta_json, configs_json=None, now_ns=None, compute_profile=true, ts_col="ts", value_col="value", quality_col=None, ingest_col=None))]
#[allow(clippy::too_many_arguments)]
fn run_checks(
    py: Python<'_>,
    data: &Bound<'_, PyAny>,
    meta_json: &str,
    configs_json: Option<&str>,
    now_ns: Option<i64>,
    compute_profile: bool,
    ts_col: &str,
    value_col: &str,
    quality_col: Option<&str>,
    ingest_col: Option<&str>,
) -> PyResult<String> {
    let frame = frame_from_py(data, meta_json, ts_col, value_col, quality_col, ingest_col)?;
    let configs: Vec<CheckConfig> = match configs_json {
        Some(s) => serde_json::from_str(s).map_err(err)?,
        None => Registry::default_configs(),
    };
    py.detach(move || {
        let mut ctx = CheckContext::from_frame(&frame);
        if let Some(now) = now_ns {
            ctx.now_ns = now;
            ctx.window = Window::new(ctx.window.start, ctx.window.end.max(now));
        }
        let profile = if compute_profile { Some(Profile::compute(&frame)) } else { None };
        if let Some(p) = &profile {
            ctx = ctx.with_profile(p.clone());
        }
        let out = Registry::run(&configs, &frame, &ctx).map_err(err)?;
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
        serde_json::to_string(&report).map_err(err)
    })
}

/// Baseline profile of an Arrow batch/stream as JSON.
#[pyfunction]
#[pyo3(signature = (data, meta_json, ts_col="ts", value_col="value", quality_col=None))]
fn profile(
    py: Python<'_>,
    data: &Bound<'_, PyAny>,
    meta_json: &str,
    ts_col: &str,
    value_col: &str,
    quality_col: Option<&str>,
) -> PyResult<String> {
    let frame = frame_from_py(data, meta_json, ts_col, value_col, quality_col, None)?;
    py.detach(move || serde_json::to_string(&Profile::compute(&frame)).map_err(err))
}

/// M4 downsampling to at most `buckets` buckets; returns a pyarrow RecordBatch (ts, value).
#[pyfunction]
#[pyo3(signature = (data, buckets, ts_col="ts", value_col="value"))]
fn downsample_m4<'py>(
    py: Python<'py>,
    data: &Bound<'_, PyAny>,
    buckets: usize,
    ts_col: &str,
    value_col: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let frame = frame_from_py(data, r#"{"id":"_"}"#, ts_col, value_col, None, None)?;
    let (sorted, _) = frame.normalized();
    let (ts, values) = py.detach(move || m4(&sorted, buckets));
    let out = SeriesFrame::with_default_quality(SeriesMeta::new("_"), ts, values).map_err(err)?;
    let batch = out.to_record_batch().map_err(err)?;
    PyRecordBatch::new(batch).into_pyarrow(py)
}

/// Deterministic synthetic series with injected faults, as a pyarrow RecordBatch.
#[pyfunction]
#[pyo3(signature = (n=1440, interval_ns=60_000_000_000, seed=42, faults=vec![]))]
fn synth<'py>(
    py: Python<'py>,
    n: usize,
    interval_ns: i64,
    seed: u64,
    faults: Vec<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let spec = SynthSpec { n, interval_ns, seed, ..SynthSpec::default() };
    let mut f = generate(&spec);
    let mut rng = Rng::new(seed ^ 0xABCD);
    let len = f.len();
    for fault in faults {
        match fault.as_str() {
            "gap" => {
                inject::gap(&mut f, len / 4, len / 20);
            }
            "flatline" => {
                inject::flatline(&mut f, len / 2, len / 10);
            }
            "spikes" => {
                inject::spikes(&mut f, &mut rng, 5, 8.0 * spec.amplitude);
            }
            "nans" => {
                inject::nans(&mut f, (3 * len) / 4, len / 40);
            }
            "negative" => {
                inject::set(&mut f, len / 8, 3, -1.0);
            }
            "range" => {
                inject::set(&mut f, (5 * len) / 8, 2, 1e6);
            }
            "duplicates" => {
                inject::duplicate(&mut f, len / 3, 2);
                inject::conflicting_duplicate(&mut f, len / 3 + 1, 1.0);
            }
            "out_of_order" => {
                inject::swap(&mut f, len / 5, len / 5 + 1);
            }
            other => return Err(PyValueError::new_err(format!("unknown fault `{other}`"))),
        }
    }
    let batch = f.to_record_batch().map_err(err)?;
    PyRecordBatch::new(batch).into_pyarrow(py)
}

#[pyfunction]
fn builtin_checks() -> Vec<String> {
    Registry::builtin_ids().iter().map(|s| s.to_string()).collect()
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(run_checks, m)?)?;
    m.add_function(wrap_pyfunction!(profile, m)?)?;
    m.add_function(wrap_pyfunction!(downsample_m4, m)?)?;
    m.add_function(wrap_pyfunction!(synth, m)?)?;
    m.add_function(wrap_pyfunction!(builtin_checks, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
