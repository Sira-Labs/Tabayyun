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
