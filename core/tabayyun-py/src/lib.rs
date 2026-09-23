//! Python bindings. Input frames arrive through the Arrow PyCapsule interface (anything with
//! `__arrow_c_stream__` or `__arrow_c_array__`: pyarrow tables/batches, polars frames, ...).
//! Results are returned as JSON strings and turned into dicts by the thin Python wrapper,
//! which keeps the binding free of any Python-object marshalling code.

use arrow::array::RecordBatch;
use arrow::compute::concat_batches;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use pyo3_arrow::{PyRecordBatch, PyRecordBatchReader};
use std::collections::BTreeMap;
use tabayyun_core::cache::{Cache, StoreConfig};
use tabayyun_core::downsample::m4;
use tabayyun_core::synth::{generate, inject, Rng, SynthSpec};
use tabayyun_core::{
    CheckConfig, CheckContext, CheckOutput, Profile, Registry, Scorer, SeriesFrame, SeriesGroup, SeriesMeta,
    Window,
};

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
        serde_json::to_string(&report_json(&frame, &ctx, profile.as_ref(), out)).map_err(err)
    })
}

/// The per-series report shape shared by `run_checks` and `run_checks_multi`.
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

/// Run single-series checks on several series and cross-series checks on their groups (spec
/// 008). `tables` maps series id to an Arrow batch/stream; `metas_json` is a JSON object of
/// series id to metadata (missing entries get `{"id": ...}`); `groups_json` a JSON list of
/// groups. `window` is `(start_ns, end_ns)`; without it the window spans the data (extended
/// to `now_ns`). Returns JSON `{"reports": {id: report}, "groups_skipped": [...]}`.
#[pyfunction]
#[pyo3(signature = (tables, metas_json, groups_json, configs_json=None, now_ns=None, window=None, compute_profile=true, ts_col="ts", value_col="value", quality_col=None, ingest_col=None))]
#[allow(clippy::too_many_arguments)]
fn run_checks_multi(
    py: Python<'_>,
    tables: &Bound<'_, PyDict>,
    metas_json: &str,
    groups_json: &str,
    configs_json: Option<&str>,
    now_ns: Option<i64>,
    window: Option<(i64, i64)>,
    compute_profile: bool,
    ts_col: &str,
    value_col: &str,
    quality_col: Option<&str>,
    ingest_col: Option<&str>,
) -> PyResult<String> {
    let mut metas: BTreeMap<String, SeriesMeta> = serde_json::from_str(metas_json).map_err(err)?;
    let groups: Vec<SeriesGroup> = serde_json::from_str(groups_json).map_err(err)?;
    let configs: Vec<CheckConfig> = match configs_json {
        Some(s) => serde_json::from_str(s).map_err(err)?,
        None => Registry::default_multi_configs(),
    };
    let mut frames = Vec::with_capacity(tables.len());
    for (key, data) in tables.iter() {
        let id: String = key.extract()?;
        let meta = metas.remove(&id).unwrap_or_else(|| SeriesMeta::new(id.clone()));
        if meta.id != id {
            return Err(PyValueError::new_err(format!("metadata for series {id} names series {}", meta.id)));
        }
        let batch = batch_from_py(&data)?;
        let frame =
            SeriesFrame::from_record_batch_ext(meta, &batch, ts_col, value_col, quality_col, ingest_col);
        frames.push(frame.map_err(err)?);
    }
    if let Some((start, end)) = window.filter(|(s, e)| s >= e) {
        return Err(PyValueError::new_err(format!("window start {start} is not before its end {end}")));
    }
    py.detach(move || {
        let data_window = || {
            let start = frames.iter().filter_map(SeriesFrame::first_ts).min().unwrap_or(0);
            let end = frames.iter().filter_map(SeriesFrame::last_ts).max().map_or(start, |t| t + 1);
            Window::new(start, end.max(now_ns.unwrap_or(i64::MIN)))
        };
        let window = window.map_or_else(data_window, |(s, e)| Window::new(s, e));
        let ctx = CheckContext { now_ns: now_ns.unwrap_or(window.end - 1), window, profile: None };
        let profiles: BTreeMap<String, Profile> = if compute_profile {
            frames.iter().map(|f| (f.meta.id.clone(), Profile::compute(f))).collect()
        } else {
            BTreeMap::new()
        };
        let mut out = Registry::run_multi(&configs, &frames, &profiles, &groups, &ctx).map_err(err)?;
        let reports: serde_json::Map<String, serde_json::Value> = frames
            .iter()
            .map(|f| {
                let output = out.per_series.remove(&f.meta.id).unwrap_or_default();
                (f.meta.id.clone(), report_json(f, &ctx, profiles.get(&f.meta.id), output))
            })
            .collect();
        let result = serde_json::json!({"reports": reports, "groups_skipped": out.groups_skipped});
        serde_json::to_string(&result).map_err(err)
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

/// Parquet cache on local disk or S3 (spec 006). One instance per process is enough; it owns
/// the store client and a small runtime, and every call releases the GIL.
#[pyclass(name = "Cache", frozen)]
struct PyCache {
    inner: Cache,
}

#[pymethods]
impl PyCache {
    /// Open a store from a JSON `StoreConfig` (`url`, `s3_endpoint`, `s3_region`,
    /// `s3_access_key_id`, `s3_secret_access_key`, `s3_allow_http`).
    #[new]
    fn new(py: Python<'_>, store_json: &str) -> PyResult<Self> {
        let cfg: StoreConfig = serde_json::from_str(store_json).map_err(err)?;
        let inner = py.detach(move || Cache::open(&cfg)).map_err(err)?;
        Ok(PyCache { inner })
    }

    /// Write one series; returns the JSON `WriteReport` (files, rows, start_ns, end_ns).
    #[pyo3(signature = (layer, source_id, data, meta_json, ts_col="ts", value_col="value", quality_col=None, ingest_col=None))]
    #[allow(clippy::too_many_arguments)]
    fn write(
        &self,
        py: Python<'_>,
        layer: &str,
        source_id: &str,
        data: &Bound<'_, PyAny>,
        meta_json: &str,
        ts_col: &str,
        value_col: &str,
        quality_col: Option<&str>,
        ingest_col: Option<&str>,
    ) -> PyResult<String> {
        let frame = frame_from_py(data, meta_json, ts_col, value_col, quality_col, ingest_col)?;
        let report = py.detach(|| self.inner.write(layer, source_id, &frame)).map_err(err)?;
        serde_json::to_string(&report).map_err(err)
    }

    /// Read series over `[start_ns, end_ns)`; returns `(series_id, pyarrow.RecordBatch)` pairs in
    /// request order (columns ts, value, quality and ingest_ts when every row has one).
    fn read<'py>(
        &self,
        py: Python<'py>,
        layer: &str,
        source_id: &str,
        series_ids: Vec<String>,
        start_ns: i64,
        end_ns: i64,
    ) -> PyResult<Vec<(String, Bound<'py, PyAny>)>> {
        let ids: Vec<&str> = series_ids.iter().map(String::as_str).collect();
        let frames = py.detach(|| self.inner.read(layer, source_id, &ids, start_ns, end_ns)).map_err(err)?;
        frames
            .into_iter()
            .map(|f| {
                let batch = f.to_record_batch().map_err(err)?;
                Ok((f.meta.id.clone(), PyRecordBatch::new(batch).into_pyarrow(py)?))
            })
            .collect()
    }
}

#[pyfunction]
fn builtin_checks() -> Vec<String> {
    Registry::builtin_ids().iter().map(|s| s.to_string()).collect()
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(run_checks, m)?)?;
    m.add_function(wrap_pyfunction!(run_checks_multi, m)?)?;
    m.add_function(wrap_pyfunction!(profile, m)?)?;
    m.add_function(wrap_pyfunction!(downsample_m4, m)?)?;
    m.add_function(wrap_pyfunction!(synth, m)?)?;
    m.add_function(wrap_pyfunction!(builtin_checks, m)?)?;
    m.add_class::<PyCache>()?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
