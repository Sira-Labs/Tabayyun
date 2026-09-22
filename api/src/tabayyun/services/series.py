"""Series and sources: the Uploads source, upload series, metadata precedence, reads, PATCH.

Spec 004. Metadata precedence for a run: a value passed with the upload overrides the stored
one for that run and is saved (the user stated a fact); a value not passed comes from the
stored series; a value in neither is derived by the core. Only the fields the core accepts
reach it; the operational band is stored for later checks and not sent yet.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun import core
from tabayyun.db.models import DEFAULT_ORG_ID, DEFAULT_WORKSPACE_ID, Finding, Score, Series, Source

log = structlog.get_logger()

UPLOADS_SOURCE_NAME = "Uploads"
UNRESOLVED_STATUSES = ("open", "acked")
MAX_METADATA_BYTES = 8 * 1024
# Upload form fields that are series facts: saved on the series when a run succeeds.
UPLOAD_METADATA_FIELDS = ("unit", "physical_min", "physical_max")
# Series columns a PATCH may set.
EDITABLE_FIELDS = (
    "name",
    "unit",
    "kind",
    "expected_interval_ns",
    "physical_min",
    "physical_max",
    "operational_min",
    "operational_max",
    "resolution",
    "non_negative",
    "asset_path",
    "metadata",
)
NOT_NULLABLE = frozenset({"name", "kind", "metadata"})


class MetadataError(ValueError):
    """Invalid series metadata; `field` names the offending field for a 422 response."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


@dataclass(frozen=True)
class SeriesStats:
    """Aggregates shown next to a series: latest score, unresolved findings, runs."""

    latest_score: dict[str, Any] | None
    open_findings: int
    n_runs: int
    last_run_at: datetime | None


# Validation


def validate_limits(values: Mapping[str, Any], changed: Sequence[str] = ()) -> None:
    """Check the physical and operational bands of a merged metadata set.

    Raises MetadataError naming a field that was changed when one is involved, so the caller
    sees the field it just sent rather than the stored one.
    """

    def blame(*fields: str) -> str:
        for field in fields:
            if field in changed:
                return field
        return fields[-1]

    pmin, pmax = values.get("physical_min"), values.get("physical_max")
    omin, omax = values.get("operational_min"), values.get("operational_max")
    if pmin is not None and pmax is not None and pmin >= pmax:
        raise MetadataError(blame("physical_max", "physical_min"), "physical_min must be below physical_max")
    if omin is not None and omax is not None and omin >= omax:
        raise MetadataError(
            blame("operational_max", "operational_min"), "operational_min must be below operational_max"
        )
    if omin is not None and pmin is not None and omin < pmin:
        raise MetadataError(blame("operational_min", "physical_min"), "operational band below physical_min")
    if omax is not None and pmax is not None and omax > pmax:
        raise MetadataError(blame("operational_max", "physical_max"), "operational band above physical_max")
    if omin is not None and pmax is not None and omin > pmax:
        raise MetadataError(blame("operational_min", "physical_max"), "operational band above physical_max")
    if omax is not None and pmin is not None and omax < pmin:
        raise MetadataError(blame("operational_max", "physical_min"), "operational band below physical_min")


def validate_metadata_blob(metadata: Any) -> None:
    """`metadata` is a JSON object of at most 8 KiB."""
    if not isinstance(metadata, dict):
        raise MetadataError("metadata", "must be a JSON object")
    if len(json.dumps(metadata, separators=(",", ":")).encode()) > MAX_METADATA_BYTES:
        raise MetadataError("metadata", f"larger than {MAX_METADATA_BYTES} bytes")


def _values(series: Series | None) -> dict[str, Any]:
    if series is None:
        return {}
    return {f: getattr(series, "metadata_" if f == "metadata" else f) for f in EDITABLE_FIELDS}


# Upload series (worker and request paths)


async def uploads_source_id(session: AsyncSession) -> uuid.UUID | None:
    """Id of the workspace's Uploads source, or None before the first upload."""
    stmt = select(Source.id).where(
        Source.workspace_id == DEFAULT_WORKSPACE_ID, Source.name == UPLOADS_SOURCE_NAME
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def find_upload_series(session: AsyncSession, external_id: str) -> Series | None:
    """The stored upload series with this external id, without creating anything."""
    source_id = await uploads_source_id(session)
    if source_id is None:
        return None
    stmt = select(Series).where(Series.source_id == source_id, Series.external_id == external_id)
    return (await session.execute(stmt)).scalar_one_or_none()


def upload_overrides(
    unit: str | None, physical_min: float | None, physical_max: float | None
) -> dict[str, Any]:
    """The metadata an upload states: only the fields it actually passed."""
    passed = {"unit": unit, "physical_min": physical_min, "physical_max": physical_max}
    return {k: v for k, v in passed.items() if v is not None}


def merged_for_run(stored: Series | None, overrides: Mapping[str, Any]) -> dict[str, Any]:
    """Stored metadata with the upload's values on top; raises MetadataError when invalid."""
    values = {**_values(stored), **overrides}
    validate_limits(values, changed=list(overrides))
    return values


def meta_for_core(external_id: str, values: Mapping[str, Any]) -> core.SeriesMetaIn:
    """The core's series metadata from merged values; absent fields are derived by the core."""
    return core.SeriesMetaIn(
        id=external_id,
        name=values.get("name") or external_id,
        unit=values.get("unit"),
        kind=values.get("kind") or "measurement",
        expected_interval_ns=values.get("expected_interval_ns"),
        physical_min=values.get("physical_min"),
        physical_max=values.get("physical_max"),
        resolution=values.get("resolution"),
        non_negative=values.get("non_negative"),
    )


async def upsert_upload_series(
    session: AsyncSession, external_id: str, overrides: Mapping[str, Any], *, now: datetime
) -> Series:
    """Create the Uploads source and the series on first use, then save the upload's metadata.

    Both inserts use ON CONFLICT DO NOTHING so concurrent runs of a new series cannot fail on
    the unique constraints.
    """
    await session.execute(
        pg_insert(Source)
        .values(
            id=uuid.uuid4(),
            org_id=DEFAULT_ORG_ID,
            workspace_id=DEFAULT_WORKSPACE_ID,
            type="upload",
            name=UPLOADS_SOURCE_NAME,
            config={},
            health={},
        )
        .on_conflict_do_nothing(index_elements=["workspace_id", "name"])
    )
    source_id = await uploads_source_id(session)
    await session.execute(
        pg_insert(Series)
        .values(
            id=uuid.uuid4(),
            org_id=DEFAULT_ORG_ID,
            workspace_id=DEFAULT_WORKSPACE_ID,
            source_id=source_id,
            external_id=external_id,
            name=external_id,
            metadata_={},
        )
        .on_conflict_do_nothing(index_elements=["source_id", "external_id"])
    )
    stmt = (
        select(Series)
        .where(Series.source_id == source_id, Series.external_id == external_id)
        .with_for_update()
    )
    series = (await session.execute(stmt)).scalar_one()
    changed = {k: v for k, v in overrides.items() if getattr(series, k) != v}
    if changed:
        for key, value in changed.items():
            setattr(series, key, value)
        series.updated_at = now
        await session.flush()
        log.info("series.metadata_saved", series_id=str(series.id), fields=sorted(changed))
    return series


# Reads


async def list_sources(session: AsyncSession) -> list[tuple[Source, int]]:
    """Sources of the workspace with their series counts, by name."""
    n_series = select(func.count()).select_from(Series).where(Series.source_id == Source.id).scalar_subquery()
    stmt = (
        select(Source, n_series)
        .where(Source.workspace_id == DEFAULT_WORKSPACE_ID)
        .order_by(Source.name, Source.id)
    )
    return [(source, int(count)) for source, count in (await session.execute(stmt)).all()]


async def get_series(session: AsyncSession, series_id: uuid.UUID) -> Series | None:
    """The series in the default workspace, or None."""
    stmt = select(Series).where(Series.id == series_id, Series.workspace_id == DEFAULT_WORKSPACE_ID)
    return (await session.execute(stmt)).scalar_one_or_none()


async def series_stats(
    session: AsyncSession, series_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, SeriesStats]:
    """Latest raw-layer score, unresolved findings, run count and last run per series."""
    if not series_ids:
        return {}
    latest = (
        select(Score.series_id, Score.overall, Score.computed_at)
        .where(Score.series_id.in_(series_ids), Score.layer == "raw")
        .distinct(Score.series_id)
        .order_by(Score.series_id, Score.computed_at.desc())
    )
    runs = (
        select(Score.series_id, func.count(func.distinct(Score.run_id)), func.max(Score.computed_at))
        .where(Score.series_id.in_(series_ids))
        .group_by(Score.series_id)
    )
    findings = (
        select(Finding.series_id, func.count())
        .where(Finding.series_id.in_(series_ids), Finding.status.in_(UNRESOLVED_STATUSES))
        .group_by(Finding.series_id)
    )
    scores = {
        sid: {"overall": overall, "computed_at": at} for sid, overall, at in await session.execute(latest)
    }
    run_rows = {sid: (int(n), last) for sid, n, last in await session.execute(runs)}
    open_counts = {sid: int(n) for sid, n in await session.execute(findings)}
    return {
        sid: SeriesStats(
            latest_score=scores.get(sid),
            open_findings=open_counts.get(sid, 0),
            n_runs=run_rows.get(sid, (0, None))[0],
            last_run_at=run_rows.get(sid, (0, None))[1],
        )
        for sid in series_ids
    }


def encode_name_cursor(series: Series) -> str:
    """Opaque keyset cursor over `(name, id)`."""
    return base64.urlsafe_b64encode(json.dumps([series.name, str(series.id)]).encode()).decode()


def decode_name_cursor(cursor: str) -> tuple[str, uuid.UUID]:
    """Inverse of `encode_name_cursor`; raises ValueError("invalid cursor")."""
    try:
        name, series_id = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        if not isinstance(name, str):
            raise ValueError("name is not a string")
        return name, uuid.UUID(series_id)
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError("invalid cursor") from exc


def _like_pattern(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def list_series(
    session: AsyncSession,
    *,
    q: str | None,
    source_id: uuid.UUID | None,
    kind: str | None,
    limit: int,
    cursor: str | None,
) -> tuple[list[Series], str | None]:
    """Series by name, keyset-paginated on `(name, id)`; `q` matches name or external id."""
    stmt = select(Series).where(Series.workspace_id == DEFAULT_WORKSPACE_ID)
    if q:
        pattern = _like_pattern(q)
        stmt = stmt.where(
            Series.name.ilike(pattern, escape="\\") | Series.external_id.ilike(pattern, escape="\\")
        )
    if source_id is not None:
        stmt = stmt.where(Series.source_id == source_id)
    if kind is not None:
        stmt = stmt.where(Series.kind == kind)
    if cursor:
        name, series_id = decode_name_cursor(cursor)
        stmt = stmt.where(tuple_(Series.name, Series.id) > (name, series_id))
    stmt = stmt.order_by(Series.name, Series.id).limit(limit + 1)
    rows = list((await session.execute(stmt)).scalars())
    next_cursor = encode_name_cursor(rows[limit - 1]) if len(rows) > limit else None
    return rows[:limit], next_cursor


# Updates


async def patch_series(
    session: AsyncSession, series_id: uuid.UUID, changes: Mapping[str, Any], *, now: datetime
) -> Series | None:
    """Apply a partial update after validating the merged result; None when not found."""
    stmt = (
        select(Series)
        .where(Series.id == series_id, Series.workspace_id == DEFAULT_WORKSPACE_ID)
        .with_for_update()
    )
    series = (await session.execute(stmt)).scalar_one_or_none()
    if series is None:
        return None
    for field in changes:
        if field not in EDITABLE_FIELDS:
            raise MetadataError(field, "not an editable field")
        if field in NOT_NULLABLE and changes[field] is None:
            raise MetadataError(field, "cannot be null")
    if "metadata" in changes:
        validate_metadata_blob(changes["metadata"])
    validate_limits({**_values(series), **changes}, changed=list(changes))
    for field, value in changes.items():
        setattr(series, "metadata_" if field == "metadata" else field, value)
    series.updated_at = now
    await session.flush()
    log.info("series.patched", series_id=str(series_id), fields=sorted(changes))
    return series
