"""The `csv_dir` connector (spec 026): config rules, reading long and wide CSV and Parquet files,
listing a folder confined to the file sources root, and the check."""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

from tabayyun import connectors
from tabayyun.connectors import BATCH_SCHEMA, ConnectorError, NetPolicy
from tabayyun.connectors.csv_dir import (
    NOT_ENABLED,
    CsvDirConfig,
    CsvDirConnector,
    FileReadError,
    TooManySeriesError,
    read_file,
)
from tabayyun.connectors.csv_dir.files import list_files, sha256

S = 1_000_000_000
T0 = 1_767_225_600 * S  # 2026-01-01T00:00:00Z
OLD = 1_700_000_000  # a modification time well before "now" (2023)


def config(**overrides) -> CsvDirConfig:
    overrides.setdefault("path", "drop")
    return CsvDirConfig.model_validate(overrides)


def write(path: Path, text: str, *, mtime: int | None = OLD) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


LONG = (
    "ts,series,value,quality\n"
    "2026-01-01T00:00:00Z,flow,1.5,good\n"
    "2026-01-01T00:01:00Z,flow,,bad\n"
    "2026-01-01T00:00:00Z,temp,20,\n"
)


# Config


@pytest.mark.parametrize("path", ["/etc", "../up", "a/../../b", "a\\b", "a\x00b", "a\nb"])
def test_paths_outside_or_odd_are_refused(path):
    with pytest.raises(ValidationError):
        config(path=path)


def test_paths_are_normalised_and_the_root_itself_is_allowed():
    assert config(path="plant/./hourly/").path == "plant/hourly"
    assert config(path=".").path == "."


def test_a_long_layout_needs_distinct_columns():
    with pytest.raises(ValidationError, match="series_column and value_column"):
        config(series_column=None)
    with pytest.raises(ValidationError, match="must differ"):
        config(value_column="ts")
    assert config(layout="wide", series_column=None, value_column=None).layout == "wide"


def test_limits_and_unknown_keys():
    for bad in ({"max_file_mb": 0}, {"max_files_per_scan": 1001}, {"delimiter": ";;"}, {"delimiter": '"'}):
        with pytest.raises(ValidationError):
            config(**bad)
    with pytest.raises(ValidationError):
        config(endpoint_url="http://x")
    assert config().settle_s == 60 and config().max_file_mb == 256


# Reading


def test_long_csv_splits_by_series_with_quality_and_nan(tmp_path):
    rows = read_file(write(tmp_path / "a.csv", LONG), config(quality_column="quality"))
    assert set(rows.series) == {"flow", "temp"} and rows.rows == 3
    flow = rows.series["flow"]
    assert flow.schema == BATCH_SCHEMA
    assert flow.column("ts").to_pylist() == [T0, T0 + 60 * S]
    assert flow.column("value").to_pylist() == [1.5, None]
    assert flow.column("quality").to_pylist() == ["good", "bad"]
    assert rows.series["temp"].column("quality").to_pylist() == [None]
    assert (rows.start_ns, rows.end_ns) == (T0, T0 + 60 * S + 1)


def test_long_without_a_quality_column_reads_good(tmp_path):
    rows = read_file(write(tmp_path / "a.csv", LONG), config())
    assert rows.series["flow"].column("quality").null_count == 2


def test_wide_csv_with_semicolons_drops_empty_cells(tmp_path):
    text = "ts;P1;P2\n1767225600;1;\n1767225660;2;3\n"
    rows = read_file(write(tmp_path / "w.csv", text), config(layout="wide", delimiter=";", ts_unit="s"))
    assert rows.series["P1"].column("value").to_pylist() == [1.0, 2.0]
    assert rows.series["P2"].column("ts").to_pylist() == [T0 + 60 * S]
    assert rows.rows == 3


def test_epoch_units_are_inferred(tmp_path):
    text = "ts,series,value\n1767225600000,a,1\n1767225660000,a,2\n"
    rows = read_file(write(tmp_path / "ms.csv", text), config())
    assert rows.series["a"].column("ts").to_pylist() == [T0, T0 + 60 * S]


def test_parquet_long_and_wide(tmp_path):
    long = pa.table(
        {
            "ts": pa.array([T0, T0 + S], pa.timestamp("ns", tz="UTC")),
            "series": ["x", "y"],
            "value": pa.array([1, 2], pa.int32()),
        }
    )
    pq.write_table(long, tmp_path / "l.parquet")
    rows = read_file(tmp_path / "l.parquet", config())
    assert rows.series["y"].column("value").to_pylist() == [2.0]
    wide = pa.table({"time": pa.array([T0], pa.int64()), "a": [1.0], "b": [None]}, schema=None)
    pq.write_table(wide, tmp_path / "w.parquet")
    rows = read_file(tmp_path / "w.parquet", config(layout="wide", ts_column="time", ts_unit="ns"))
    assert rows.series["a"].num_rows == 1 and rows.series["b"].num_rows == 0


@pytest.mark.parametrize(
    ("text", "overrides", "message"),
    [
        ("ts,series\n2026-01-01T00:00:00Z,a\n", {}, "column 'value' is missing"),
        ("time,series,value\n2026-01-01T00:00:00Z,a,1\n", {}, "column 'ts' is missing"),
        ("ts,series,value\nyesterday,a,1\n", {}, "ts"),
        ("ts,series,value\n,a,1\n2026-01-01T00:00:00Z,a,2\n", {}, "1 row(s) have no timestamp"),
        ("ts,series,value\n2026-01-01T00:00:00Z, ,1\n", {}, "1 row(s) have no series id"),
        ("ts,series,value\n2026-01-01T00:00:00Z," + "x" * 300 + ",1\n", {}, "longer than 256"),
        ("ts,series,value\n2026-01-01T00:00:00Z,a,high\n", {}, "cannot read the CSV"),
        ("ts,a\n2026-01-01T00:00:00Z,high\n", {"layout": "wide"}, "column 'a' is not numeric"),
        ("ts\n2026-01-01T00:00:00Z\n", {"layout": "wide"}, "at least one column"),
        ("ts,series,value\n0,a,1\n", {"ts_unit": "s"}, "outside 1971"),
    ],
)
def test_malformed_files_name_what_is_wrong(tmp_path, text, overrides, message):
    with pytest.raises(FileReadError) as caught:
        read_file(write(tmp_path / "bad.csv", text), config(**overrides))
    assert message in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


def test_too_many_series(tmp_path):
    text = "ts,series,value\n" + "".join(f"2026-01-01T00:00:00Z,s{i},1\n" for i in range(3))
    with pytest.raises(TooManySeriesError, match="3 series"):
        read_file(write(tmp_path / "many.csv", text), config(max_series_per_file=2))
    with pytest.raises(TooManySeriesError):
        read_file(
            write(tmp_path / "w.csv", "ts,a,b,c\n2026-01-01T00:00:00Z,1,2,3\n"),
            config(layout="wide", max_series_per_file=2),
        )


def test_not_a_parquet_file(tmp_path):
    with pytest.raises(FileReadError, match="not a readable Parquet file"):
        read_file(write(tmp_path / "x.parquet", "not parquet"), config())


# Listing


def test_listing_picks_matching_settled_files_oldest_first(tmp_path):
    root = tmp_path
    base = root / "drop"
    write(base / "b.csv", "x", mtime=OLD + 10)
    write(base / "a.csv", "x", mtime=OLD + 20)
    write(base / "c.parquet", "x", mtime=OLD)
    write(base / "notes.txt", "x")
    write(base / ".hidden.csv", "x")
    write(base / "copy.csv.part", "x")
    write(base / "~$lock.csv", "x")
    write(base / "fresh.csv", "x", mtime=None)
    write(base / "sub" / "deep.csv", "x")
    listing = list_files(root, base, config(), time.time_ns())
    assert [f.path for f in listing.files] == ["c.parquet", "b.csv", "a.csv"]
    assert listing.settling == 1 and listing.matched == 4


def test_listing_recurses_without_hidden_folders_and_applies_the_pattern(tmp_path):
    base = tmp_path / "drop"
    write(base / "sub" / "deep.csv", "x")
    write(base / ".git" / "x.csv", "x")
    write(base / "other.csv", "x")
    listing = list_files(tmp_path, base, config(recursive=True, pattern="d*.csv"), time.time_ns())
    assert [f.path for f in listing.files] == ["sub/deep.csv"]


def test_large_files_and_links_out_of_the_root_are_skipped(tmp_path):
    root = tmp_path / "root"
    base = root / "drop"
    write(base / "big.csv", "x" * (1024 * 1024 + 1))
    outside = write(tmp_path / "secret.csv", "x")
    (base / "link.csv").symlink_to(outside)
    inside = write(root / "shared" / "ok.csv", "x")
    (base / "fine.csv").symlink_to(inside)
    listing = list_files(root, base, config(max_file_mb=1), time.time_ns())
    assert listing.too_large == ["big.csv"]
    assert listing.outside_root == ["link.csv"]
    assert [f.path for f in listing.files] == ["fine.csv"]


def test_sha256_is_of_the_content(tmp_path):
    a = write(tmp_path / "a.csv", "same")
    b = write(tmp_path / "b.csv", "same")
    assert sha256(a) == sha256(b) != sha256(write(tmp_path / "c.csv", "other"))


# The connector


def build(root: Path | None, **overrides) -> CsvDirConnector:
    overrides.setdefault("path", "drop")
    conn = connectors.build("csv_dir", overrides, None, NetPolicy(), files_root=root)
    assert isinstance(conn, CsvDirConnector)
    return conn


def test_check_counts_the_files(tmp_path):
    write(tmp_path / "drop" / "a.csv", "12345")
    write(tmp_path / "drop" / "b.csv", "123", mtime=None)
    assert asyncio.run(build(tmp_path).check()) == {"files": 2, "bytes": 8}


@pytest.mark.parametrize(
    ("root", "path", "message"),
    [
        (None, "drop", NOT_ENABLED),
        ("missing-root", "drop", "file sources folder of this server does not exist"),
        (".", "nope", "folder nope does not exist"),
        (".", "file.csv", "file.csv is not a folder"),
    ],
)
def test_check_refuses(tmp_path, root, path, message):
    write(tmp_path / "file.csv", "x")
    resolved = None if root is None else (tmp_path / root).resolve()
    with pytest.raises(ConnectorError, match=re.escape(message)) as caught:
        asyncio.run(build(resolved, path=path).check())
    assert not caught.value.retryable


def test_a_link_to_a_folder_outside_the_root_is_refused(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (root / "drop").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ConnectorError, match="outside the file sources folder"):
        asyncio.run(build(root.resolve()).check())


def test_it_reads_no_windows():
    conn = build(Path("/tmp"))
    assert not conn.reads_windows and not conn.supports("search") and not conn.supports("describe")

    async def go():
        return [b async for b in conn.fetch([], 0, 1)]

    with pytest.raises(ConnectorError, match="cannot be fetched by window"):
        asyncio.run(go())
