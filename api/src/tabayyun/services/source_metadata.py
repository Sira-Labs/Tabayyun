"""Metadata imported from a connector into series (spec 022).

Rules, per series:

1. An imported value fills a column only when it is empty, or always with `overwrite`; a value
   the system does not know never clears one.
2. Columns: `unit` (at most 32 characters), the physical and operational limits, `asset_path`
   (at most 512 characters). Values that are too long or not finite are skipped.
3. The merged limits are validated as a PATCH is (spec 004); if they fail, the limit changes are
   skipped and the reason noted, the other fields still apply.
4. `metadata[<source type>]` is replaced with the description, the connector's `extra` and the
   import time, unless it would push the blob past 8 KiB.

Pure functions over the ORM object: the caller owns the transaction and the audit event.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tabayyun.connectors import PointMetadata
from tabayyun.db.models import Series
from tabayyun.services.series import MetadataError, validate_limits, validate_metadata_blob

MAX_UNIT = 32
MAX_ASSET_PATH = 512
LIMIT_FIELDS = ("physical_min", "physical_max", "operational_min", "operational_max")
COLUMNS = ("unit", *LIMIT_FIELDS, "asset_path")


@dataclass
class Applied:
    """What an import changed on one series, and what it skipped and why."""

    changed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def as_result(self) -> dict[str, Any]:
        """The series' entry in the job's result."""
        out: dict[str, Any] = {"changed": self.changed}
        if self.skipped:
            out["skipped"] = self.skipped
        return out


def _candidates(meta: PointMetadata, skipped: list[str]) -> dict[str, Any]:
    """The columns the import offers, cleaned; reasons for dropped ones go to `skipped`."""
    out: dict[str, Any] = {}
    unit = (meta.unit or "").strip()
    if unit:
        if len(unit) > MAX_UNIT:
            skipped.append(f"unit: longer than {MAX_UNIT} characters")
        else:
            out["unit"] = unit
    for name in LIMIT_FIELDS:
        value = getattr(meta, name)
        if value is None:
            continue
        if not math.isfinite(value):
            skipped.append(f"{name}: not a finite number")
            continue
        out[name] = float(value)
    path = (meta.asset_path or "").strip()
    if path:
        if len(path) > MAX_ASSET_PATH:
            skipped.append(f"asset_path: longer than {MAX_ASSET_PATH} characters")
        else:
            out["asset_path"] = path
    return out


def _blob(meta: PointMetadata, now: datetime) -> dict[str, Any]:
    blob: dict[str, Any] = {k: v for k, v in meta.extra.items() if v is not None}
    if meta.description:
        blob["description"] = meta.description
    blob["imported_at"] = now.isoformat()
    return blob


def apply(
    series: Series, meta: PointMetadata, *, source_type: str, overwrite: bool, now: datetime
) -> Applied:
    """Apply imported metadata to `series` in place by the rules above."""
    result = Applied()
    current = {name: getattr(series, name) for name in COLUMNS}
    wanted = {
        name: value
        for name, value in _candidates(meta, result.skipped).items()
        if overwrite or current[name] is None
    }
    merged = {**current, **wanted}
    try:
        validate_limits(merged, [n for n in wanted if n in LIMIT_FIELDS])
    except MetadataError as exc:
        result.skipped.append(f"limits: {exc.message}")
        wanted = {n: v for n, v in wanted.items() if n not in LIMIT_FIELDS}
    for name, value in wanted.items():
        if current[name] != value:
            setattr(series, name, value)
            result.changed.append(name)

    stored = dict(series.metadata_ or {})
    previous = dict(stored.get(source_type) or {})
    blob = _blob(meta, now)
    candidate = {**stored, source_type: blob}
    try:
        validate_metadata_blob(candidate)
    except MetadataError as exc:
        result.skipped.append(f"metadata: {exc.message}")
        return result
    previous.pop("imported_at", None)
    if _canonical(previous) != _canonical({k: v for k, v in blob.items() if k != "imported_at"}):
        result.changed.append("metadata")
    # A new dict, so the JSONB column is seen as changed.
    series.metadata_ = candidate
    return result


def _canonical(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
