"""Reading one dropped file into per-series tables (spec 026).

A file is CSV (`.csv`) or Parquet (`.parquet`), in one of two layouts:

- **long:** one row per sample; a column names the series, one holds the value, and an
  optional one the quality text;
- **wide:** one row per time; every column except the timestamp is a series named by its header.
  An empty cell is no sample.

Timestamps follow the upload rules (`tabayyun.core.normalise_ts`). Everything that makes a file
unreadable raises `FileReadError` with a message naming the column, fit to show to users.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from tabayyun import core
from tabayyun.connectors.base import BATCH_SCHEMA
from tabayyun.connectors.csv_dir.config import CsvDirConfig

MAX_SERIES_ID = 256
NULL_VALUES = ["", "NA", "NaN", "nan", "null"]
TIMESTAMP_PARSERS = [pacsv.ISO8601, "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"]


class FileReadError(Exception):
    """The file cannot be read as configured; the message is shown to users."""


class TooManySeriesError(FileReadError):
    """The file names more series than the source allows per file."""


@dataclass(frozen=True)
class FileRows:
    """A file's samples: a `BATCH_SCHEMA` table per series id, and their totals."""

    series: dict[str, pa.Table]
    rows: int
    start_ns: int | None
    end_ns: int | None


def read_file(path: Path, config: CsvDirConfig) -> FileRows:
    """The samples of one file; blocking.

    Raises:
        FileReadError: a missing or malformed column, a row without a timestamp, a bad series
            id, or too many series (`TooManySeriesError`).
    """
    table = _read_parquet(path, config) if path.suffix.lower() == ".parquet" else _read_csv(path, config)
    table = _timestamps(table, config)
    series = _long(table, config) if config.layout == "long" else _wide(table, config)
    if len(series) > config.max_series_per_file:
        raise _too_many(len(series), config)
    rows = sum(t.num_rows for t in series.values())
    bounds = [pc.min_max(t.column("ts")) for t in series.values() if t.num_rows]
    start = min((b["min"].as_py() for b in bounds), default=None)
    end = max((b["max"].as_py() for b in bounds), default=None)
    return FileRows(series=series, rows=rows, start_ns=start, end_ns=None if end is None else end + 1)


def _too_many(n: int, config: CsvDirConfig) -> TooManySeriesError:
    limit = config.max_series_per_file
    return TooManySeriesError(f"the file names {n} series, more than max_series_per_file ({limit})")


def _needed(config: CsvDirConfig) -> list[str]:
    """The columns a long layout reads; a wide one reads them all."""
    if config.layout == "wide":
        return []
    columns = [config.ts_column, config.series_column, config.value_column]
    if config.quality_column is not None:
        columns.append(config.quality_column)
    return [c for c in columns if c is not None]


def _require(present: list[str], needed: list[str]) -> None:
    missing = [c for c in needed if c not in present]
    if missing:
        raise FileReadError(f"column {missing[0]!r} is missing")


def _read_csv(path: Path, config: CsvDirConfig) -> pa.Table:
    parse = pacsv.ParseOptions(delimiter=config.delimiter)
    try:
        header = pacsv.open_csv(path, parse_options=parse).schema.names
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError) as exc:
        raise FileReadError(f"not a readable CSV file: {_first_line(exc)}") from None
    needed = _needed(config) or header
    _require(header, [config.ts_column, *needed])
    column_types: dict[str, pa.DataType] = {}
    if config.layout == "long":
        assert config.series_column is not None and config.value_column is not None
        column_types = {config.series_column: pa.string(), config.value_column: pa.float64()}
        if config.quality_column is not None:
            column_types[config.quality_column] = pa.string()
    try:
        return pacsv.read_csv(
            path,
            parse_options=parse,
            convert_options=pacsv.ConvertOptions(
                include_columns=needed,
                column_types=column_types,
                null_values=NULL_VALUES,
                strings_can_be_null=True,
                timestamp_parsers=TIMESTAMP_PARSERS,
            ),
        )
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError) as exc:
        raise FileReadError(f"cannot read the CSV: {_first_line(exc)}") from None


def _read_parquet(path: Path, config: CsvDirConfig) -> pa.Table:
    try:
        names = pq.read_schema(path).names
        needed = _needed(config) or names
        _require(names, [config.ts_column, *needed])
        return pq.read_table(path, columns=needed)
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError, OSError) as exc:
        raise FileReadError(f"not a readable Parquet file: {_first_line(exc)}") from None


def _first_line(exc: BaseException) -> str:
    """An Arrow message's first line, which names the row or column; never a server path."""
    return str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__


def _timestamps(table: pa.Table, config: CsvDirConfig) -> pa.Table:
    """The timestamp column as int64 ns since the epoch, every row with one."""
    name = config.ts_column
    try:
        table, _unit = core.normalise_ts(table, name, config.ts_unit)
    except ValueError as exc:
        raise FileReadError(str(exc)) from None
    ts = table.column(name)
    if pa.types.is_timestamp(ts.type):
        ts = ts.cast(pa.int64())
    missing = ts.null_count
    if missing:
        raise FileReadError(f"{missing} row(s) have no timestamp in column {name!r}")
    return table.set_column(table.schema.get_field_index(name), name, ts)


def _values(column: pa.ChunkedArray, name: str) -> pa.ChunkedArray:
    try:
        return pc.cast(column, pa.float64())
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError):
        raise FileReadError(f"column {name!r} is not numeric") from None


def _series_id(value: str) -> str:
    if len(value) > MAX_SERIES_ID:
        raise FileReadError(f"a series id is longer than {MAX_SERIES_ID} characters: {value[:40]!r}…")
    return value


def _long(table: pa.Table, config: CsvDirConfig) -> dict[str, pa.Table]:
    """Split a long table by its series column."""
    assert config.series_column is not None and config.value_column is not None
    ids = pc.utf8_trim_whitespace(pc.cast(table.column(config.series_column), pa.string()))
    empty = pc.sum(pc.or_kleene(pc.is_null(ids), pc.equal(ids, ""))).as_py() or 0
    if empty:
        raise FileReadError(f"{empty} row(s) have no series id in column {config.series_column!r}")
    values = _values(table.column(config.value_column), config.value_column)
    if config.quality_column is not None:
        quality = pc.cast(table.column(config.quality_column), pa.string())
    else:
        quality = pa.chunked_array([pa.nulls(table.num_rows, pa.string())])
    samples = pa.table({"id": ids, "ts": table.column(config.ts_column), "value": values, "quality": quality})
    unique = pc.unique(samples.column("id")).to_pylist()
    if len(unique) > config.max_series_per_file:
        raise _too_many(len(unique), config)
    ordered = samples.take(pc.sort_indices(samples, [("id", "ascending"), ("ts", "ascending")]))
    counts = pc.value_counts(ordered.column("id"))
    out: dict[str, pa.Table] = {}
    offset = 0
    for item in counts.to_pylist():
        name, n = item["values"], item["counts"]
        part = ordered.slice(offset, n).select(["ts", "value", "quality"])
        out[_series_id(name)] = part.cast(BATCH_SCHEMA)
        offset += n
    return out


def _wide(table: pa.Table, config: CsvDirConfig) -> dict[str, pa.Table]:
    """One table per column other than the timestamp, without its empty cells."""
    columns = [c for c in table.column_names if c != config.ts_column]
    if not columns:
        raise FileReadError("a wide file needs at least one column besides the timestamp")
    if len(columns) > config.max_series_per_file:
        raise _too_many(len(columns), config)
    ts = table.column(config.ts_column)
    out: dict[str, pa.Table] = {}
    for name in columns:
        series_id = name.strip()
        if not series_id:
            raise FileReadError("a column has no header")
        if series_id in out:
            raise FileReadError(f"column {series_id!r} appears twice")
        values = _values(table.column(name), name)
        present = pc.is_valid(values)
        part = pa.table(
            {
                "ts": ts.filter(present),
                "value": values.filter(present),
                "quality": pa.nulls(int(pc.sum(present).as_py() or 0), pa.string()),
            }
        )
        out[_series_id(series_id)] = part.cast(BATCH_SCHEMA).sort_by("ts")
    return out
