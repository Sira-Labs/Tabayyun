"""Adapter between the API and the Rust core (``tabayyun_core`` wheel).

Keeps all knowledge of the core's JSON shapes in one place so routers work with typed models.
"""

from __future__ import annotations

from typing import Any, Literal

import pyarrow as pa
import pyarrow.csv as pacsv
import tabayyun_core as tc
from pydantic import BaseModel, Field

SeriesKind = Literal["measurement", "counter", "setpoint", "status"]


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
    id: str = Field(min_length=1, max_length=128)
    params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class Finding(BaseModel):
    check_id: str
    series_id: str
    dimension: str
    severity: str
    window: dict[str, int]
    score_impact: float
    summary: str
    evidence: dict[str, Any]


class Score(BaseModel):
    series_id: str
    method_version: str
    overall: float
    dimensions: dict[str, float]
    n_findings: int


class CheckReport(BaseModel):
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


def read_csv(
    data: bytes, ts_col: str, value_col: str, quality_col: str | None, ingest_col: str | None = None
) -> pa.Table:
    """Parse an uploaded CSV into an Arrow table with a UTC timestamp column.

    Timestamps may be ISO 8601 strings (with or without offset) or epoch integers; values are
    parsed as float64 with empty cells as null; the quality column is read as text.
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
    ts = table.column(ts_col)
    if pa.types.is_timestamp(ts.type):
        ts = (
            ts.cast(pa.timestamp("ns", tz="UTC"))
            if ts.type.tz
            else ts.cast(pa.timestamp("ns")).cast(pa.timestamp("ns", tz="UTC"))
        )
    elif pa.types.is_integer(ts.type):
        ts = ts.cast(pa.int64())
    else:
        raise ValueError(f"column {ts_col!r} is not a timestamp or integer column ({ts.type})")
    return table.set_column(table.schema.get_field_index(ts_col), ts_col, ts)
