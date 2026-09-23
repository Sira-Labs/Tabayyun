"""Parquet cache access for runs (spec 006).

The worker (and the API in inline mode) holds one `RunCache`. It opens the store on first
use, so a misconfigured or unreachable store fails the cache step of a run, never the process
start or the run itself.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pyarrow as pa
import tabayyun_core

from tabayyun.settings import Settings

RAW_LAYER = "raw"


def store_config(settings: Settings) -> dict[str, Any]:
    """The core's `StoreConfig` from settings; the secret leaves `SecretStr` only here."""
    secret = settings.s3_secret_access_key
    return {
        "url": settings.cache_url,
        "s3_endpoint": settings.s3_endpoint,
        "s3_region": settings.s3_region,
        "s3_access_key_id": settings.s3_access_key_id,
        "s3_secret_access_key": secret.get_secret_value() if secret else None,
        "s3_allow_http": settings.s3_allow_http,
    }


@dataclass(frozen=True)
class CacheWrite:
    """What one series write stored."""

    rows: int
    files: int
    start_ns: int
    end_ns: int


class CacheError(Exception):
    """The store could not be opened, read or written; `str()` is one line for run stats."""


def _cache_error(exc: BaseException) -> CacheError:
    """One-line CacheError from any error the core raised."""
    lines = str(exc).strip().splitlines()
    return CacheError((lines[0] if lines else type(exc).__name__)[:500])


class RunCache:
    """Lazily opened, thread-safe handle on the Parquet cache (the core releases the GIL)."""

    def __init__(self, store: dict[str, Any]) -> None:
        self._store = store
        self._cache: tabayyun_core.Cache | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> RunCache:
        """Handle for the store the settings name."""
        return cls(store_config(settings))

    def _open(self) -> tabayyun_core.Cache:
        with self._lock:
            if self._cache is None:
                self._cache = tabayyun_core.Cache(self._store)
            return self._cache

    def write_series(
        self,
        *,
        source_id: str,
        series_id: str,
        table: pa.Table,
        ts_col: str,
        value_col: str,
        quality_col: str | None,
        ingest_col: str | None,
    ) -> CacheWrite:
        """Write one series to the raw layer; blocking, call it from a thread.

        Raises:
            CacheError: the store cannot be opened or the write failed, whatever the cause.
        """
        try:
            report = self._open().write(
                RAW_LAYER,
                source_id,
                table,
                {"id": series_id},
                ts_col=ts_col,
                value_col=value_col,
                quality_col=quality_col,
                ingest_col=ingest_col,
            )
        # The core maps store and Parquet errors to ValueError, but the cache step runs after the
        # run succeeded and must never fail it: any ordinary error (a panic surfacing as
        # RuntimeError, a TypeError from a bad store config) becomes a CacheError.
        except Exception as exc:  # noqa: BLE001
            raise _cache_error(exc) from exc
        return CacheWrite(
            rows=int(report["rows"]),
            files=len(report["files"]),
            start_ns=int(report["start_ns"]),
            end_ns=int(report["end_ns"]),
        )

    def read_series(
        self, *, source_id: str, series_ids: Sequence[str], start_ns: int, end_ns: int
    ) -> dict[str, pa.RecordBatch]:
        """Raw-layer rows of series of one source over `[start_ns, end_ns)`; blocking.

        Returns a batch (columns ts, value, quality and ingest_ts when every row has one) per
        series that has rows; series without rows are absent.

        Raises:
            CacheError: the store cannot be opened or read, whatever the cause.
        """
        try:
            batches = self._open().read(RAW_LAYER, source_id, list(series_ids), start_ns, end_ns)
        # A dataset run cannot proceed without its data; the caller fails the run with this line.
        except Exception as exc:  # noqa: BLE001
            raise _cache_error(exc) from exc
        return {sid: b for sid, b in batches.items() if b.num_rows > 0}
