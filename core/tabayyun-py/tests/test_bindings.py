import polars as pl
import pyarrow as pa
import pytest

import tabayyun_core as tc


def test_builtin_checks_listed():
    ids = tc.builtin_checks()
    assert "tby.completeness" in ids and "tby.spikes" in ids
    assert len(ids) >= 14


def test_synth_and_run_checks_pyarrow():
    batch = tc.synth(n=2880, faults=["gap", "flatline", "nans", "negative"])
    assert isinstance(batch, pa.RecordBatch)
    report = tc.run_checks(batch, {"id": "demo", "unit": "m3/h"}, quality_col="quality")
    found = {f["check_id"] for f in report["findings"]}
    assert {"tby.completeness", "tby.flatline", "tby.value_type", "tby.non_negative"} <= found
    assert 0 <= report["score"]["overall"] < 100
    assert report["profile"]["expected_interval_ns"] == 60_000_000_000


def test_polars_input_and_configs():
    df = pl.from_arrow(tc.synth(n=600))
    assert isinstance(df, pl.DataFrame)
    report = tc.run_checks(df, {"id": "p"}, configs=[{"id": "tby.completeness"}, {"id": "tby.staleness"}])
    assert report["findings"] == []
    assert {m["check_id"] for m in report["metrics"]} == {"tby.completeness", "tby.staleness"}


def test_staleness_with_now():
    batch = tc.synth(n=100)
    last = batch.column("ts")[-1].value  # ns
    report = tc.run_checks(batch, {"id": "s"}, configs=[{"id": "tby.staleness"}], now_ns=last + 3 * 3600 * 10**9)
    assert [f["check_id"] for f in report["findings"]] == ["tby.staleness"]


def test_profile_and_downsample():
    batch = tc.synth(n=10_000)
    prof = tc.profile(batch, {"id": "x"})
    assert prof["n_samples"] == 10_000 and prof["noise_mad"] > 0
    small = tc.downsample_m4(batch, 250)
    assert isinstance(small, pa.RecordBatch)
    assert 0 < small.num_rows <= 1000


def test_chart_points_gaps_and_quality():
    batch = tc.synth(n=10_000, faults=["gap"])
    ts = [t.value for t in batch.column("ts")]
    quality = pa.array(["bad" if 100 <= i < 200 else "good" for i in range(len(ts))])
    table = pa.table({"ts": batch.column("ts"), "value": batch.column("value"), "quality": quality})
    start, end = ts[0], ts[-1] + 1
    out = tc.chart(table, start, end, 200)
    assert set(out) == {"ts", "values", "quality", "n_raw"}
    assert out["n_raw"] == len(ts)
    assert 0 < len(out["ts"]) <= 4 * 200 + 50
    assert out["ts"] == sorted(out["ts"])
    assert any(v != v for v in out["values"]), "the injected gap breaks the line"
    assert [q[2] for q in out["quality"]] == ["bad"]
    assert out["quality"][0][0] <= ts[100] < ts[199] < out["quality"][0][1]
    with pytest.raises(ValueError):
        tc.chart(table, end, start, 200)


def _series(values, *, step=60 * 10**9, quality=None):
    ts = pa.array([i * step for i in range(len(values))], type=pa.timestamp("ns", tz="UTC"))
    quality = pa.array(quality or ["good"] * len(values))
    return pa.table({"ts": ts, "value": pa.array(values, type=pa.float64()), "quality": quality})


def test_compare_series_stats_do_not_depend_on_the_width():
    n = 5_000
    values = [float(i % 100) for i in range(n)]
    quality = ["bad" if i < 10 else "good" for i in range(n)]
    table = _series(values, quality=quality)
    end = n * 60 * 10**9
    wide, narrow = tc.compare_series(table, 0, end, 800), tc.compare_series(table, 0, end, 20)
    assert set(wide) == {"ts", "values", "n_raw", "stats"}
    assert wide["n_raw"] == narrow["n_raw"] == n
    assert len(narrow["ts"]) < len(wide["ts"]) < n
    assert wide["stats"] == narrow["stats"]
    stats = wide["stats"]
    assert (stats["min"], stats["max"]) == (0.0, 99.0)
    assert (stats["first_ts"], stats["first_value"]) == (10 * 60 * 10**9, 10.0)
    usable = values[10:]
    assert stats["mean"] == pytest.approx(sum(usable) / len(usable))
    # Only bad samples in the window: points to draw, nothing to normalise with.
    bad = tc.compare_series(table, 0, 10 * 60 * 10**9, 50)
    assert bad["n_raw"] == 10 and len(bad["ts"]) == 10 and bad["stats"] is None
    with pytest.raises(ValueError):
        tc.compare_series(table, end, 0, 50)


def test_residual_of_a_balance_and_a_pair():
    n = 600
    tables = {
        "in": _series([300.0] * n),
        "a": _series([98.0] * n),
        "b": _series([98.0] * n),
        "c": _series([68.6 if 200 <= i < 400 else 98.0 for i in range(n)]),
    }
    members = [("in", 1.0), ("a", -1.0), ("b", -1.0), ("c", -1.0)]
    end = n * 60 * 10**9
    grid = tc.residual(tables, {}, members, 0, end)
    assert set(grid) == {"grid_ns", "missing", "ts", "residual", "sum_in"}
    assert grid["grid_ns"] == 60 * 10**9 and grid["missing"] == [] and len(grid["ts"]) == n
    assert grid["sum_in"] == [300.0] * n
    chart = tc.residual(tables, {}, members, 0, end, buckets=1000, loss=(-0.01, 0.05))
    assert set(chart) == {"grid_ns", "missing", "ts", "residual", "lo", "hi"}
    outside = [r < lo or r > hi for r, lo, hi in zip(chart["residual"], chart["lo"], chart["hi"])]
    assert outside == [200 <= i < 400 for i in range(n)]

    pair = tc.residual(tables, {}, [("a", 1.0), ("c", -1.0)], 0, end, buckets=1000)
    assert set(pair) == {"grid_ns", "missing", "ts", "residual"}
    assert pair["residual"][0] == 0.0 and pair["residual"][300] == pytest.approx(29.4)

    tables["b"] = _series([98.0] * n, quality=["bad"] * n)
    missing = tc.residual(tables, {}, members, 0, end, buckets=1000, loss=(-0.01, 0.05))
    assert missing["missing"] == ["b"] and missing["ts"] == [] and missing["residual"] == []
    coarse = tc.residual(tables, {}, [("a", 1.0), ("c", -1.0)], 0, end, grid="1h")
    assert coarse["grid_ns"] == 3600 * 10**9 and len(coarse["ts"]) == n // 60
    with pytest.raises(ValueError, match="grid: cannot parse duration"):
        tc.residual(tables, {}, members, 0, end, grid="soon")
    with pytest.raises(ValueError, match="no table for member x"):
        tc.residual(tables, {}, [("a", 1.0), ("x", -1.0)], 0, end)


def test_latency_with_ingest_column():
    batch = tc.synth(n=300)
    ts = batch.column("ts")
    late = pa.array([t.value + 10 * 60 * 10**9 for t in ts], type=pa.timestamp("ns", tz="UTC"))
    table = pa.table({"ts": ts, "value": batch.column("value"), "arrived": late})
    report = tc.run_checks(table, {"id": "l"}, configs=[{"id": "tby.latency"}], ingest_col="arrived")
    assert [f["check_id"] for f in report["findings"]] == ["tby.latency"]
    assert report["score"]["method_version"] == "v2"


def test_bad_input_raises():
    with pytest.raises(ValueError):
        tc.run_checks(pa.table({"a": [1, 2]}), {"id": "x"})


def test_cache_round_trip_pyarrow(tmp_path):
    cache = tc.Cache({"url": str(tmp_path / "cache")})
    batch = tc.synth(n=24 * 60)  # one day of minutes
    report = cache.write("raw", "src", batch, {"id": "series-1"}, quality_col="quality")
    assert report["rows"] == 24 * 60 and len(report["files"]) >= 1
    ts = batch.column("ts")
    start, end = ts[60].value, ts[120].value
    got = cache.read("raw", "src", ["series-1", "missing"], start, end)
    assert list(got) == ["series-1", "missing"]
    part = got["series-1"]
    assert isinstance(part, pa.RecordBatch) and part.num_rows == 60
    assert part.column("ts")[0].value == start
    assert part.column("value").to_pylist() == batch.column("value").slice(60, 60).to_pylist()
    assert got["missing"].num_rows == 0


def test_cache_rejects_unknown_scheme(tmp_path):
    with pytest.raises(ValueError, match="unsupported"):
        tc.Cache({"url": "gs://bucket"})


def test_run_checks_multi():
    tables = {sid: tc.synth(n=1440, seed=seed) for sid, seed in (("pt-a", 1), ("pt-b", 2), ("pt-c", 3))}
    groups = [
        {"id": "g-pt", "name": "PT-101", "kind": "redundant", "members": [{"series_id": "pt-a"}, {"series_id": "pt-b"}]},
        {"id": "g-gone", "name": "Gone", "kind": "related", "members": [{"series_id": "pt-c"}, {"series_id": "pt-x"}]},
    ]
    out = tc.run_checks_multi(tables, {"pt-a": {"id": "pt-a", "unit": "bar"}}, groups, quality_col="quality")
    assert set(out["reports"]) == {"pt-a", "pt-b", "pt-c"}
    assert out["groups_skipped"] == [{"group_id": "g-gone", "reason": "members without data", "missing": ["pt-x"]}]
    # Each report has the single-series shape and matches a single run of that series.
    single = tc.run_checks(tables["pt-b"], {"id": "pt-b"}, quality_col="quality")
    multi = out["reports"]["pt-b"]
    assert multi["findings"] == single["findings"] and multi["score"] == single["score"]
    assert multi["window"] == single["window"]
    assert out["reports"]["pt-a"]["profile"]["expected_interval_ns"] == 60_000_000_000


def test_run_checks_multi_uses_given_profiles():
    """A series with a stored baseline is judged against it, the others against themselves."""
    tables = {sid: tc.synth(n=1440, seed=seed) for sid, seed in (("pt-a", 1), ("pt-b", 2))}
    configs = [{"id": "tby.rate_of_change"}]
    own = tc.run_checks_multi(tables, configs=configs, quality_col="quality")["reports"]
    assert all(r["findings"] == [] for r in own.values())
    # A baseline that knows only tiny rates: ordinary steps of pt-a now exceed 3 x p99.9.
    calm = dict(own["pt-a"]["profile"], rate_p999=own["pt-a"]["profile"]["rate_p999"] / 1000)
    out = tc.run_checks_multi(
        tables, configs=configs, quality_col="quality", profiles={"pt-a": calm, "pt-gone": calm}
    )["reports"]
    assert {f["check_id"] for f in out["pt-a"]["findings"]} == {"tby.rate_of_change"}
    assert out["pt-a"]["profile"]["rate_p999"] == calm["rate_p999"]
    assert out["pt-b"] == own["pt-b"]
    # A stored profile written before a field existed still loads.
    old = {k: v for k, v in calm.items() if k not in ("noise_mad", "quantiles")}
    again = tc.run_checks_multi(tables, configs=configs, quality_col="quality", profiles={"pt-a": old})
    assert again["reports"]["pt-a"]["findings"] == out["pt-a"]["findings"]
    with pytest.raises(ValueError):
        tc.run_checks_multi(tables, quality_col="quality", profiles={"pt-a": {"n_samples": "many"}})


def test_run_checks_multi_window_and_errors():
    batch = tc.synth(n=60)
    start = batch.column("ts")[0].value
    window = (start, start + 2 * 3600 * 10**9)
    out = tc.run_checks_multi({"a": batch}, configs=[{"id": "tby.completeness"}], window=window, now_ns=window[1] - 1)
    report = out["reports"]["a"]
    assert report["window"] == {"start": window[0], "end": window[1]}
    # One hour of data in a two-hour window: completeness is judged against the window.
    assert {f["check_id"] for f in report["findings"]} == {"tby.completeness"}
    with pytest.raises(ValueError, match="names series"):
        tc.run_checks_multi({"a": batch}, {"a": {"id": "b"}})
    with pytest.raises(ValueError, match="not before"):
        tc.run_checks_multi({"a": batch}, window=(5, 5))
    with pytest.raises(ValueError, match="invalid group"):
        tc.run_checks_multi({"a": batch}, groups=[{"id": "g", "name": "G", "kind": "balance", "members": [{"series_id": "a", "role": "input"}]}])


def test_check_catalogue_and_validate_configs():
    catalogue = tc.check_catalogue()
    assert [c["id"] for c in catalogue][:21] == tc.builtin_checks()
    assert len(catalogue) == 24 and {c["scope"] for c in catalogue[21:]} == {"group"}
    by_id = {c["id"]: c for c in catalogue}
    spikes = by_id["tby.spikes"]
    assert spikes["scope"] == "series" and spikes["defaults"]["min_scale"] is None
    assert spikes["auto"]["min_scale"]["source"] == "baseline"
    assert by_id["tby.physical_range"]["auto"]["min"]["source"] == "metadata"
    assert by_id["tby.balance_residual"]["defaults"]["loss_max"] == 0.05

    tc.validate_configs([{"id": "tby.spikes", "params": {"window": 21}}, {"id": "tby.balance_residual"}])
    with pytest.raises(ValueError, match="unknown check id: tby.nope"):
        tc.validate_configs([{"id": "tby.nope"}])
    with pytest.raises(ValueError, match="invalid parameters for tby.spikes"):
        tc.validate_configs([{"id": "tby.spikes", "params": {"window": "wide"}}])
