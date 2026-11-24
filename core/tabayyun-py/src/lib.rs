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
use tabayyun_core::cache::{catch_panic, Cache, StoreConfig};
use tabayyun_core::downsample::{gap_breaks, m4, m4_window, quality_runs, window_stats};
use tabayyun_core::frame::modal_interval;
use tabayyun_core::repair::{repair as apply_repair, Action, Op};
use tabayyun_core::residual::{residual as group_residual, residual_chart};
use tabayyun_core::scatter::scatter as pair_scatter;
use tabayyun_core::synth::{generate, inject, Rng, SynthSpec};
use tabayyun_core::time::parse_duration;
use tabayyun_core::{
    CheckConfig, CheckContext, CheckOutput, Profile, Quality, Registry, Scorer, SeriesFrame, SeriesGroup,
    SeriesMeta, Window,
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
/// to `now_ns`). `profiles_json` is a JSON object of series id to a stored baseline profile
/// (spec 028): those series are checked against it, any other against the profile of its own
/// data when `compute_profile` is set. Returns JSON `{"reports": {id: report},
/// "groups_skipped": [...]}`.
#[pyfunction]
#[pyo3(signature = (tables, metas_json, groups_json, configs_json=None, now_ns=None, window=None, compute_profile=true, ts_col="ts", value_col="value", quality_col=None, ingest_col=None, profiles_json=None))]
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
    profiles_json: Option<&str>,
) -> PyResult<String> {
    let stored: BTreeMap<String, Profile> = match profiles_json {
        Some(s) => serde_json::from_str(s).map_err(err)?,
        None => BTreeMap::new(),
    };
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
        let mut profiles = stored;
        profiles.retain(|id, _| frames.iter().any(|f| &f.meta.id == id));
        if compute_profile {
            for f in &frames {
                if !profiles.contains_key(&f.meta.id) {
                    profiles.insert(f.meta.id.clone(), Profile::compute(f));
                }
            }
        }
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

/// M4 points of a sorted frame on `buckets` bins of `[from_ns, to_ns)`, with a NaN where the
/// line breaks across a gap, and the number of raw samples in the window.
fn chart_points(
    frame: &SeriesFrame,
    from_ns: i64,
    to_ns: i64,
    buckets: usize,
    expected_interval_ns: Option<i64>,
) -> (Vec<i64>, Vec<f64>, usize) {
    let lo = frame.ts.partition_point(|&t| t < from_ns);
    let hi = frame.ts.partition_point(|&t| t < to_ns);
    // The interval only sets where gaps break the line, so a sample of the window will do.
    let interval =
        expected_interval_ns.or_else(|| modal_interval(&frame.ts[lo..hi.max(lo).min(lo + 10_000)]));
    let bin = ((to_ns as i128 - from_ns as i128) / buckets as i128).max(1) as i64;
    let max_step = (2 * bin).max(interval.map_or(0, |i| i.saturating_mul(3)));
    let (ts, values) = m4_window(frame, from_ns, to_ns, buckets);
    let (ts, values) = gap_breaks(&ts, &values, max_step);
    (ts, values, hi.saturating_sub(lo))
}

/// Chart points of one series over `[from_ns, to_ns)` (spec 025): M4 on `buckets` window bins
/// with gap breaks, plus the runs of non-good quality. Returns a dict with `ts` (list of int),
/// `values` (list of float, NaN where the line breaks), `quality` (list of `(start, end, class)`)
/// and `n_raw` (samples in the window).
#[pyfunction]
#[pyo3(signature = (data, from_ns, to_ns, buckets, expected_interval_ns=None, ts_col="ts", value_col="value", quality_col=Some("quality")))]
#[allow(clippy::too_many_arguments)]
fn chart<'py>(
    py: Python<'py>,
    data: &Bound<'_, PyAny>,
    from_ns: i64,
    to_ns: i64,
    buckets: usize,
    expected_interval_ns: Option<i64>,
    ts_col: &str,
    value_col: &str,
    quality_col: Option<&str>,
) -> PyResult<Bound<'py, PyDict>> {
    if buckets == 0 || to_ns <= from_ns {
        return Err(PyValueError::new_err("chart needs buckets > 0 and from_ns < to_ns"));
    }
    let frame = frame_from_py(data, r#"{"id":"_"}"#, ts_col, value_col, quality_col, None)?;
    let (ts, values, runs, n_raw) = py.detach(move || {
        let frame = if frame.is_sorted() { frame } else { frame.normalized().0 };
        let (ts, values, n_raw) = chart_points(&frame, from_ns, to_ns, buckets, expected_interval_ns);
        (ts, values, quality_runs(&frame, from_ns, to_ns, buckets), n_raw)
    });
    let out = PyDict::new(py);
    out.set_item("ts", ts)?;
    out.set_item("values", values)?;
    let runs: Vec<(i64, i64, &str)> = runs
        .into_iter()
        .map(|r| {
            let class = match r.quality {
                Quality::Good => "good",
                Quality::Uncertain => "uncertain",
                Quality::Bad => "bad",
                Quality::Estimated => "estimated",
            };
            (r.start, r.end, class)
        })
        .collect();
    out.set_item("quality", runs)?;
    out.set_item("n_raw", n_raw)?;
    Ok(out)
}

/// One series of the compare view over `[from_ns, to_ns)` (spec 018): the chart points of
/// [`chart`] and the statistics of the window's usable, finite raw samples. Returns a dict
/// with `ts`, `values`, `n_raw` and `stats` (`min`, `max`, `mean`, `sd`, `first_ts`,
/// `first_value`), which is `None` when the window holds no such sample.
#[pyfunction]
#[pyo3(signature = (data, from_ns, to_ns, buckets, expected_interval_ns=None, ts_col="ts", value_col="value", quality_col=Some("quality")))]
#[allow(clippy::too_many_arguments)]
fn compare_series<'py>(
    py: Python<'py>,
    data: &Bound<'_, PyAny>,
    from_ns: i64,
    to_ns: i64,
    buckets: usize,
    expected_interval_ns: Option<i64>,
    ts_col: &str,
    value_col: &str,
    quality_col: Option<&str>,
) -> PyResult<Bound<'py, PyDict>> {
    if buckets == 0 || to_ns <= from_ns {
        return Err(PyValueError::new_err("compare_series needs buckets > 0 and from_ns < to_ns"));
    }
    let frame = frame_from_py(data, r#"{"id":"_"}"#, ts_col, value_col, quality_col, None)?;
    let (ts, values, n_raw, stats) = py.detach(move || {
        let frame = if frame.is_sorted() { frame } else { frame.normalized().0 };
        let (ts, values, n_raw) = chart_points(&frame, from_ns, to_ns, buckets, expected_interval_ns);
        (ts, values, n_raw, window_stats(&frame, from_ns, to_ns))
    });
    let out = PyDict::new(py);
    out.set_item("ts", ts)?;
    out.set_item("values", values)?;
    out.set_item("n_raw", n_raw)?;
    match stats {
        Some(s) => {
            let stats = PyDict::new(py);
            stats.set_item("min", s.min)?;
            stats.set_item("max", s.max)?;
            stats.set_item("mean", s.mean)?;
            stats.set_item("sd", s.sd)?;
            stats.set_item("first_ts", s.first_ts)?;
            stats.set_item("first_value", s.first_value)?;
            out.set_item("stats", stats)?;
        }
        None => out.set_item("stats", py.None())?,
    }
    Ok(out)
}

/// Residual of a balance or a pair over `[from_ns, to_ns)` (spec 018).
///
/// `members` lists `(series_id, sign)` in order: `+1` for inputs and a pair's first member,
/// `-1` for outputs and the second member. `tables` maps each id to its Arrow data and
/// `metas_json` to its metadata. `grid` is `auto` (the coarsest expected interval among the
/// members, the default) or a duration such as `15m`, as in the checks' params. Returns a dict with `grid_ns`, `missing`
/// (members without a usable, finite sample in the window), `ts` and `residual`. Without
/// `buckets` these are per grid bin, with `sum_in`. With `buckets` they are chart points on
/// that many bins of the window, with the band `lo` and `hi` when `loss` gives
/// `(loss_min, loss_max)`.
#[pyfunction]
#[pyo3(signature = (tables, metas_json, members, from_ns, to_ns, grid=None, buckets=None, loss=None, ts_col="ts", value_col="value", quality_col=Some("quality")))]
#[allow(clippy::too_many_arguments)]
fn residual<'py>(
    py: Python<'py>,
    tables: &Bound<'_, PyDict>,
    metas_json: &str,
    members: Vec<(String, f64)>,
    from_ns: i64,
    to_ns: i64,
    grid: Option<&str>,
    buckets: Option<usize>,
    loss: Option<(f64, f64)>,
    ts_col: &str,
    value_col: &str,
    quality_col: Option<&str>,
) -> PyResult<Bound<'py, PyDict>> {
    let mut metas: BTreeMap<String, SeriesMeta> = serde_json::from_str(metas_json).map_err(err)?;
    let mut frames = Vec::with_capacity(members.len());
    for (id, _) in &members {
        let data =
            tables.get_item(id)?.ok_or_else(|| PyValueError::new_err(format!("no table for member {id}")))?;
        let meta = metas.remove(id).unwrap_or_else(|| SeriesMeta::new(id.clone()));
        let batch = batch_from_py(&data)?;
        let frame = SeriesFrame::from_record_batch_ext(meta, &batch, ts_col, value_col, quality_col, None);
        frames.push(frame.map_err(err)?);
    }
    let signs: Vec<f64> = members.iter().map(|(_, sign)| *sign).collect();
    let grid_ns = grid_ns_of(grid)?;
    let (r, chart) = py.detach(move || {
        let frames: Vec<SeriesFrame> =
            frames.into_iter().map(|f| if f.is_sorted() { f } else { f.normalized().0 }).collect();
        let r = group_residual(&frames.iter().collect::<Vec<_>>(), &signs, grid_ns, from_ns, to_ns)
            .map_err(err)?;
        let chart = buckets.map(|b| residual_chart(&r, from_ns, to_ns, b, loss)).transpose().map_err(err)?;
        Ok::<_, PyErr>((r, chart))
    })?;
    let out = PyDict::new(py);
    out.set_item("grid_ns", r.grid_ns)?;
    out.set_item("missing", r.missing)?;
    match chart {
        Some(c) => {
            out.set_item("ts", c.ts)?;
            out.set_item("residual", c.residual)?;
            if loss.is_some() {
                out.set_item("lo", c.lo)?;
                out.set_item("hi", c.hi)?;
            }
        }
        None => {
            out.set_item("ts", r.ts)?;
            out.set_item("residual", r.residual)?;
            out.set_item("sum_in", r.sum_in)?;
        }
    }
    Ok(out)
}

/// The frame's samples in time order, all of them: samples that share a timestamp stay, in
/// their order, so a bin's mean does not depend on the order the rows arrived in.
fn sorted_by_time(frame: SeriesFrame) -> PyResult<SeriesFrame> {
    if frame.is_sorted() {
        return Ok(frame);
    }
    let mut idx: Vec<usize> = (0..frame.len()).collect();
    idx.sort_by_key(|&i| frame.ts[i]);
    SeriesFrame::new(
        frame.meta.clone(),
        idx.iter().map(|&i| frame.ts[i]).collect(),
        idx.iter().map(|&i| frame.values[i]).collect(),
        idx.iter().map(|&i| frame.quality[i]).collect(),
    )
    .map_err(err)
}

/// `grid` of a call: `None` for `auto` or nothing, else the duration in ns.
fn grid_ns_of(grid: Option<&str>) -> PyResult<Option<i64>> {
    match grid.filter(|g| *g != "auto") {
        Some(g) => Ok(Some(parse_duration(g).filter(|ns| *ns > 0).ok_or_else(|| {
            PyValueError::new_err(format!("grid: cannot parse duration `{g}` (use e.g. 90s, 15m, 2h, 1d)"))
        })?)),
        None => Ok(None),
    }
}

/// One series against another over `[from_ns, to_ns)` on the checks' grid (spec 033).
///
/// `tables` maps the ids `x` and `y` to their Arrow data and `metas_json` to their metadata.
/// `grid` is `auto` (the coarser expected interval of the two, the default) or a duration.
/// Returns a dict with `grid_ns`, `n_bins` (grid bins in which both have a value), `r` (the
/// Pearson correlation over those bins, or None), at most `max_points` points as `ts`, `x`
/// and `y`, and `missing` (series without a usable, finite sample in the window).
#[pyfunction]
#[pyo3(signature = (tables, metas_json, x, y, from_ns, to_ns, grid=None, max_points=2000, ts_col="ts", value_col="value", quality_col=Some("quality")))]
#[allow(clippy::too_many_arguments)]
fn scatter<'py>(
    py: Python<'py>,
    tables: &Bound<'_, PyDict>,
    metas_json: &str,
    x: &str,
    y: &str,
    from_ns: i64,
    to_ns: i64,
    grid: Option<&str>,
    max_points: usize,
    ts_col: &str,
    value_col: &str,
    quality_col: Option<&str>,
) -> PyResult<Bound<'py, PyDict>> {
    let mut metas: BTreeMap<String, SeriesMeta> = serde_json::from_str(metas_json).map_err(err)?;
    let mut frames = Vec::with_capacity(2);
    for id in [x, y] {
        let data =
            tables.get_item(id)?.ok_or_else(|| PyValueError::new_err(format!("no table for series {id}")))?;
        let meta = metas.remove(id).unwrap_or_else(|| SeriesMeta::new(id.to_string()));
        let batch = batch_from_py(&data)?;
        let frame = SeriesFrame::from_record_batch_ext(meta, &batch, ts_col, value_col, quality_col, None);
        frames.push(frame.map_err(err)?);
    }
    let grid_ns = grid_ns_of(grid)?;
    let s = py.detach(move || {
        let frames: Vec<SeriesFrame> = frames.into_iter().map(sorted_by_time).collect::<PyResult<_>>()?;
        pair_scatter(&frames[0], &frames[1], grid_ns, from_ns, to_ns, max_points).map_err(err)
    })?;
    let out = PyDict::new(py);
    out.set_item("grid_ns", s.grid_ns)?;
    out.set_item("n_bins", s.n_bins)?;
    out.set_item("r", s.r)?;
    out.set_item("ts", s.ts)?;
    out.set_item("x", s.x)?;
    out.set_item("y", s.y)?;
    out.set_item("missing", s.missing)?;
    Ok(out)
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

/// Apply one repair operation to `[start_ns, end_ns)` of a series (specs 034 and 035).
///
/// `op_json` is the operation, `{"method": "impute.linear", "max_gap_ns": 3600000000000}`.
/// The rows are sorted by time first; samples that share a timestamp stay. Returns a dict
/// with `method`, `unrepaired`, the counts `changed`, `added`, `removed`, `rows` (of the
/// window after the repair), `ranges` (JSON: the touched timestamps as at most `max_ranges`
/// runs) and `window`, the repaired rows inside the window as a `pyarrow.RecordBatch`.
#[pyfunction]
#[pyo3(signature = (data, meta_json, start_ns, end_ns, op_json, ts_col="ts", value_col="value", quality_col=None, max_ranges=1000))]
#[allow(clippy::too_many_arguments)]
fn repair<'py>(
    py: Python<'py>,
    data: &Bound<'_, PyAny>,
    meta_json: &str,
    start_ns: i64,
    end_ns: i64,
    op_json: &str,
    ts_col: &str,
    value_col: &str,
    quality_col: Option<&str>,
    max_ranges: usize,
) -> PyResult<Bound<'py, PyDict>> {
    let op: Op = serde_json::from_str(op_json).map_err(err)?;
    let frame = frame_from_py(data, meta_json, ts_col, value_col, quality_col, None)?;
    let (repaired, window, ranges) = py.detach(move || {
        let frame = sorted_by_time(frame)?;
        let repaired = apply_repair(&frame, Window::new(start_ns, end_ns), &op).map_err(err)?;
        let ts = &repaired.frame.ts;
        let (lo, hi) = (ts.partition_point(|&t| t < start_ns), ts.partition_point(|&t| t < end_ns));
        let window = SeriesFrame::new(
            repaired.frame.meta.clone(),
            ts[lo..hi].to_vec(),
            repaired.frame.values[lo..hi].to_vec(),
            repaired.frame.quality[lo..hi].to_vec(),
        )
        .map_err(err)?;
        let ranges = repaired.lineage.ranges(ts, max_ranges);
        Ok::<_, PyErr>((repaired, window, ranges))
    })?;
    let out = PyDict::new(py);
    out.set_item("method", repaired.method)?;
    out.set_item("unrepaired", repaired.unrepaired)?;
    out.set_item("changed", repaired.lineage.count(Action::Changed))?;
    out.set_item("added", repaired.lineage.count(Action::Added))?;
    out.set_item("removed", repaired.lineage.count(Action::Removed))?;
    out.set_item("rows", window.len())?;
    out.set_item("ranges", serde_json::to_string(&ranges).map_err(err)?)?;
    out.set_item("window", PyRecordBatch::new(window.to_record_batch().map_err(err)?).into_pyarrow(py)?)?;
    Ok(out)
}

/// Raise `ValueError` when an operation is unknown or refuses its parameters for a series
/// with this metadata (spec 035). The operation runs on three samples one interval apart.
#[pyfunction]
fn validate_repair(meta_json: &str, op_json: &str) -> PyResult<()> {
    let op: Op = serde_json::from_str(op_json).map_err(err)?;
    let meta: SeriesMeta = serde_json::from_str(meta_json).map_err(err)?;
    let step = meta.expected_interval_ns.filter(|ns| *ns > 0).unwrap_or(60_000_000_000);
    // A day of samples, so that an operation on a coarser grid still has whole bins.
    let n = (86_400_000_000_000 / step).clamp(3, 1440);
    let ts: Vec<i64> = (0..n).map(|i| i * step).collect();
    let frame = SeriesFrame::with_default_quality(meta, ts, vec![0.0; n as usize]).map_err(err)?;
    apply_repair(&frame, Window::new(0, n * step), &op).map(|_| ()).map_err(err)
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
        let inner = py.detach(move || catch_panic(|| Cache::open(&cfg))).map_err(err)?;
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
        let report = py.detach(|| catch_panic(|| self.inner.write(layer, source_id, &frame))).map_err(err)?;
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
        let frames = py
            .detach(|| catch_panic(|| self.inner.read(layer, source_id, &ids, start_ns, end_ns)))
            .map_err(err)?;
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

/// Every built-in check with its scope, default parameters and run-time parameters, as JSON
/// (spec 027).
#[pyfunction]
fn check_catalogue() -> PyResult<String> {
    serde_json::to_string(&Registry::catalogue()).map_err(err)
}

/// Raise `ValueError` when a configured check does not exist or refuses its parameters.
#[pyfunction]
fn validate_configs(configs_json: &str) -> PyResult<()> {
    let configs: Vec<CheckConfig> = serde_json::from_str(configs_json).map_err(err)?;
    Registry::validate(&configs).map_err(err)
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(run_checks, m)?)?;
    m.add_function(wrap_pyfunction!(run_checks_multi, m)?)?;
    m.add_function(wrap_pyfunction!(profile, m)?)?;
    m.add_function(wrap_pyfunction!(downsample_m4, m)?)?;
    m.add_function(wrap_pyfunction!(chart, m)?)?;
    m.add_function(wrap_pyfunction!(compare_series, m)?)?;
    m.add_function(wrap_pyfunction!(residual, m)?)?;
    m.add_function(wrap_pyfunction!(scatter, m)?)?;
    m.add_function(wrap_pyfunction!(repair, m)?)?;
    m.add_function(wrap_pyfunction!(validate_repair, m)?)?;
    m.add_function(wrap_pyfunction!(synth, m)?)?;
    m.add_function(wrap_pyfunction!(builtin_checks, m)?)?;
    m.add_function(wrap_pyfunction!(check_catalogue, m)?)?;
    m.add_function(wrap_pyfunction!(validate_configs, m)?)?;
    m.add_class::<PyCache>()?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
