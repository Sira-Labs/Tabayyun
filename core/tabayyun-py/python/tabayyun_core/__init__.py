"""Tabayyun core: Arrow-native time-series data-quality checks implemented in Rust.

Any object implementing the Arrow PyCapsule interface (`__arrow_c_stream__` or
`__arrow_c_array__`) is accepted as input: pyarrow tables and record batches, polars
DataFrames, pandas (>=2.2) via pyarrow, nanoarrow, ...
"""

from __future__ import annotations

import json
from typing import Any

from tabayyun_core import _native

__version__: str = _native.__version__

__all__ = ["builtin_checks", "downsample_m4", "profile", "run_checks", "synth", "__version__"]


def builtin_checks() -> list[str]:
    """Ids of the built-in checks in catalogue order."""
    return list(_native.builtin_checks())


def run_checks(
    data: Any,
    meta: dict[str, Any] | str,
    configs: list[dict[str, Any]] | None = None,
    *,
    now_ns: int | None = None,
    compute_profile: bool = True,
    ts_col: str = "ts",
    value_col: str = "value",
    quality_col: str | None = None,
    ingest_col: str | None = None,
) -> dict[str, Any]:
    """Run checks on one series and return the report as a dict.

    ``meta`` needs at least ``{"id": ...}``; optional keys: ``unit``, ``kind``,
    ``expected_interval_ns``, ``physical_min``, ``physical_max``, ``resolution``,
    ``non_negative``. ``configs`` is a list of ``{"id": ..., "params": {...}}``; ``None`` runs
    every built-in check with defaults. ``ingest_col`` names an arrival-time column and
    enables the latency check.
    """
    meta_json = meta if isinstance(meta, str) else json.dumps(meta)
    configs_json = None if configs is None else json.dumps(configs)
    return json.loads(
        _native.run_checks(
            data,
            meta_json,
            configs_json,
            now_ns,
            compute_profile,
            ts_col,
            value_col,
            quality_col,
            ingest_col,
        )
    )


def profile(
    data: Any,
    meta: dict[str, Any] | str,
    *,
    ts_col: str = "ts",
    value_col: str = "value",
    quality_col: str | None = None,
) -> dict[str, Any]:
    """Baseline profile (sampling interval, robust statistics, noise floor, quality mix)."""
    meta_json = meta if isinstance(meta, str) else json.dumps(meta)
    return json.loads(_native.profile(data, meta_json, ts_col, value_col, quality_col))


def downsample_m4(data: Any, buckets: int, *, ts_col: str = "ts", value_col: str = "value") -> Any:
    """M4 downsampling for charts; returns a pyarrow RecordBatch with ``ts`` and ``value``."""
    return _native.downsample_m4(data, buckets, ts_col, value_col)


def synth(n: int = 1440, interval_ns: int = 60_000_000_000, seed: int = 42, faults: list[str] | None = None) -> Any:
    """Deterministic synthetic series (pyarrow RecordBatch) with optional injected faults."""
    return _native.synth(n, interval_ns, seed, list(faults or []))
