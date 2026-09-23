"""Cache coverage: which time ranges of a series the Parquet cache holds (spec 006).

One `coverage` row per write; `missing_ranges` answers what a run still has to fetch.
Connectors (S9-1) fill the gaps it returns.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.db.models import Coverage
from tabayyun.services.timeconv import datetime_to_ns, ns_to_datetime, ns_to_datetime_ceil


def missing_ranges(covered: list[tuple[int, int]], start_ns: int, end_ns: int) -> list[tuple[int, int]]:
    """Gaps of half-open `[start_ns, end_ns)` not covered by any half-open range, in order.

    Overlapping and touching ranges coalesce; an empty result means fully covered.
    """
    if end_ns <= start_ns:
        return []
    gaps: list[tuple[int, int]] = []
    cursor = start_ns
    for lo, hi in sorted(r for r in covered if r[1] > r[0]):
        if hi <= cursor:
            continue
        if lo >= end_ns:
            break
        if lo > cursor:
            gaps.append((cursor, lo))
        cursor = max(cursor, hi)
        if cursor >= end_ns:
            return gaps
    if cursor < end_ns:
        gaps.append((cursor, end_ns))
    return gaps


async def record(
    session: AsyncSession,
    *,
    series_id: uuid.UUID,
    start_ns: int,
    end_ns: int,
    rows: int,
    now: datetime,
    layer: str = "raw",
) -> None:
    """Record that `[start_ns, end_ns)` of a series is cached.

    Stored bounds widen to whole microseconds (start down, end up) like finding windows. A
    second write starting at the same instant widens the row instead of failing on the key.
    """
    stmt = insert(Coverage).values(
        series_id=series_id,
        layer=layer,
        range_start=ns_to_datetime(start_ns),
        range_end=ns_to_datetime_ceil(end_ns),
        rows=rows,
        written_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Coverage.series_id, Coverage.layer, Coverage.range_start],
        set_={
            "range_end": func.greatest(Coverage.range_end, stmt.excluded.range_end),
            "rows": stmt.excluded.rows,
            "written_at": stmt.excluded.written_at,
        },
    )
    await session.execute(stmt)


async def covered_ranges(
    session: AsyncSession, series_id: uuid.UUID, layer: str = "raw"
) -> list[tuple[int, int]]:
    """All recorded ranges of a series as ns pairs, oldest first."""
    rows = await session.execute(
        select(Coverage.range_start, Coverage.range_end)
        .where(Coverage.series_id == series_id, Coverage.layer == layer)
        .order_by(Coverage.range_start)
    )
    return [(datetime_to_ns(s), datetime_to_ns(e)) for s, e in rows.all()]
