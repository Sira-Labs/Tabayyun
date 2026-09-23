"""Adapter between the API and the Rust core (``tabayyun_core`` wheel).

Keeps all knowledge of the core's JSON shapes in one place so routers work with typed models.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import tabayyun_core as tc
from pydantic import BaseModel, Field

SeriesKind = Literal["measurement", "counter", "setpoint", "status"]
TsUnit = Literal["auto", "s", "ms", "us", "ns"]

# Epoch integer timestamps (ADR-0014): the unit is declared or inferred from the magnitude of
# the median value with the same thresholds as the CLI's `parse_ts`
# (core/tabayyun-cli/src/main.rs), then checked against a plausible date range.
NS_PER_UNIT = {"s": 1_000_000_000, "ms": 1_000_000, "us": 1_000, "ns": 1}
UNIT_NAMES = {"s": "seconds", "ms": "milliseconds", "us": "microseconds", "ns": "nanoseconds"}
EPOCH_INT_MIN_NS = int(datetime(1971, 1, 1, tzinfo=UTC).timestamp()) * 1_000_000_000
EPOCH_INT_MAX_NS = int(datetime(2200, 1, 1, tzinfo=UTC).timestamp()) * 1_000_000_000
TEXT_TIMESTAMPS = "text"


class TimestampUnitError(ValueError):
    """Epoch integers that do not convert to plausible dates with the declared or inferred unit."""


@dataclass(frozen=True)
class ParsedCsv:
    """An uploaded CSV as an Arrow table plus the unit its timestamp column was read in."""

    table: pa.Table
    ts_unit: str  # s, ms, us, ns for epoch integers; `text` for RFC 3339 / naive text


def infer_epoch_unit(magnitude: int) -> str:
    """Unit of an epoch integer from its magnitude (same thresholds as the CLI)."""
    if magnitude < 100_000_000_000:
        return "s"
    if magnitude < 100_000_000_000_000:
        return "ms"
    if magnitude < 100_000_000_000_000_000:
        return "us"
    return "ns"


def _fmt_ns(ns: int) -> str:
    """Date of an ns timestamp for error messages."""
    return datetime.fromtimestamp(ns / 1e9, tz=UTC).strftime("%Y-%m-%d")


def epoch_to_ns(column: pa.ChunkedArray | pa.Array, name: str, unit: TsUnit) -> tuple[pa.ChunkedArray, str]:
    """Convert an integer epoch column to ns since the epoch; returns the column and its unit.

    Raises TimestampUnitError when the result overflows or falls outside 1971–2199: a date in
    1970 or far in the future means the unit is wrong, and pre-1971 data must use RFC 3339 text.
    """
    ints = pc.cast(column, pa.int64())
    present = pc.drop_null(ints)
    if len(present) == 0:
        return pa.chunked_array([ints]) if isinstance(
            ints, pa.Array
        ) else ints, "ns" if unit == "auto" else unit
    resolved: str = unit
    if unit == "auto":
        median = pc.approximate_median(pc.abs(present)).as_py()
        resolved = infer_epoch_unit(int(median or 0))
    hint = "set ts_unit to s, ms, us or ns, or use RFC 3339 text"
    try:
        ns = pc.multiply_checked(ints, NS_PER_UNIT[resolved])
    except pa.ArrowInvalid as exc:
        raise TimestampUnitError(
            f"column {name!r}: epoch integers read as {UNIT_NAMES[resolved]} overflow; {hint}"
        ) from exc
    bounds = pc.min_max(ns)
    low, high = bounds["min"].as_py(), bounds["max"].as_py()
    if low < EPOCH_INT_MIN_NS or high >= EPOCH_INT_MAX_NS:
        raise TimestampUnitError(
            f"column {name!r}: epoch integers read as {UNIT_NAMES[resolved]} give dates from "
            f"{_fmt_ns(low)} to {_fmt_ns(high)}, outside 1971–2199; {hint}"
        )
    return (pa.chunked_array([ns]) if isinstance(ns, pa.Array) else ns), resolved


class SeriesMetaIn(BaseModel):
    """Series metadata accepted from clients; mirrors ``SeriesMeta`` in the core."""

    id: str = Field(min_length=1, max_length=256)
    name: str | None = None
    unit: str | None = Field(default=None, max_length=32)
    kind: SeriesKind = "measurement"
    expected_interval_ns: int | None = Field(default=None, gt=0)
    physical_min: float | None = None
    physical_max: float | None = None
    resolution: float | None = Field(default=None, gt=0)
    non_negative: bool | None = None


class CheckConfigIn(BaseModel):
    """One check's configuration override passed to the core."""

    id: str = Field(min_length=1, max_length=128)
    params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class Finding(BaseModel):
    """One finding as the core reports it; `window` bounds are ns since the epoch."""

    check_id: str
    series_id: str
    dimension: str
    severity: str
    window: dict[str, int]
    score_impact: float
    summary: str
    evidence: dict[str, Any]


class Score(BaseModel):
    """The core's quality score of one series over the evaluated window."""

    series_id: str
    method_version: str
    overall: float
    dimensions: dict[str, float]
    n_findings: int


class CheckReport(BaseModel):
    """The core's report for one series: findings, metrics, score, skipped checks."""

    series_id: str
    n_samples: int
    window: dict[str, int]
    now_ns: int
    score: Score
    findings: list[Finding]
    metrics: list[dict[str, Any]]
    skipped: list[dict[str, str]]
    profile: dict[str, Any] | None = None


def builtin_checks() -> list[str]:
    """Ids of the checks the core ships."""
    return list(tc.builtin_checks())


def run_checks(
    table: pa.Table | pa.RecordBatch,
    meta: SeriesMetaIn,
    configs: list[CheckConfigIn] | None = None,
    *,
    now_ns: int | None = None,
    ts_col: str = "ts",
    value_col: str = "value",
    quality_col: str | None = None,
    ingest_col: str | None = None,
) -> CheckReport:
    """Run the core checks on one series held in an Arrow table."""
    raw = tc.run_checks(
        table,
        meta.model_dump(exclude_none=True),
        None if configs is None else [c.model_dump() for c in configs],
        now_ns=now_ns,
        ts_col=ts_col,
        value_col=value_col,
        quality_col=quality_col,
        ingest_col=ingest_col,
    )
    return CheckReport.model_validate(raw)


class GroupSkip(BaseModel):
    """A series group the core did not run, and why."""

    group_id: str
    reason: str
    missing: list[str]


class MultiReport(BaseModel):
    """The core's multi-series result: one report per series and the skipped groups."""

    reports: dict[str, CheckReport]
    groups_skipped: list[GroupSkip]


def run_checks_multi(
    tables: Mapping[str, pa.Table | pa.RecordBatch],
    metas: Mapping[str, SeriesMetaIn],
    groups: list[dict[str, Any]],
    *,
    window: tuple[int, int],
    now_ns: int,
    quality_col: str | None = None,
    ingest_col: str | None = None,
) -> MultiReport:
    """Run single-series checks on every table and cross-series checks on `groups` (spec 008).

    Keys of `tables` and `metas` are the series ids the reports and groups use; `window` is
    the half-open `[start_ns, end_ns)` completeness and scores are measured against.
    """
    raw = tc.run_checks_multi(
        dict(tables),
        {sid: m.model_dump(exclude_none=True) for sid, m in metas.items()},
        groups,
        now_ns=now_ns,
        window=window,
        quality_col=quality_col,
        ingest_col=ingest_col,
    )
    return MultiReport.model_validate(raw)


def read_csv(
    data: bytes,
    ts_col: str,
    value_col: str,
    quality_col: str | None,
    ingest_col: str | None = None,
    ts_unit: TsUnit = "auto",
) -> ParsedCsv:
    """Parse an uploaded CSV into an Arrow table with a UTC timestamp column.

    Timestamps may be ISO 8601 strings (with or without offset) or epoch integers in the
    declared `ts_unit` (inferred from their magnitude with `auto`); values are parsed as
    float64 with empty cells as null; the quality column is read as text. The ingest column
    follows the same rules, its unit inferred on its own under `auto`.
    """
    columns = (
        [ts_col, value_col] + ([quality_col] if quality_col else []) + ([ingest_col] if ingest_col else [])
    )
    column_types: dict[str, pa.DataType] = {value_col: pa.float64()}
    if quality_col:
        column_types[quality_col] = pa.string()
    table = pacsv.read_csv(
        pa.BufferReader(data),
        convert_options=pacsv.ConvertOptions(
            include_columns=columns,
            column_types=column_types,
            null_values=["", "NA", "NaN", "nan", "null"],
            strings_can_be_null=True,
            timestamp_parsers=[pacsv.ISO8601, "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"],
        ),
    )
    table, resolved = _normalise_ts(table, ts_col, ts_unit)
    if ingest_col:
        table, _ = _normalise_ts(table, ingest_col, ts_unit)
    return ParsedCsv(table=table, ts_unit=resolved)


def _normalise_ts(table: pa.Table, name: str, unit: TsUnit) -> tuple[pa.Table, str]:
    """Timestamp text to UTC ns timestamps, epoch integers to int64 ns; returns the unit read."""
    ts = table.column(name)
    if pa.types.is_timestamp(ts.type):
        ts = (
            ts.cast(pa.timestamp("ns", tz="UTC"))
            if ts.type.tz
            else ts.cast(pa.timestamp("ns")).cast(pa.timestamp("ns", tz="UTC"))
        )
        resolved = TEXT_TIMESTAMPS
    elif pa.types.is_integer(ts.type):
        ts, resolved = epoch_to_ns(ts, name, unit)
    else:
        raise ValueError(f"column {name!r} is not a timestamp or integer column ({ts.type})")
    return table.set_column(table.schema.get_field_index(name), name, ts), resolved
