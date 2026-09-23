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
