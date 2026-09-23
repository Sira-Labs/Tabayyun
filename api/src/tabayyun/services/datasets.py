"""Datasets (spec 008): the series and the time window a dataset run covers.

A dataset stores an explicit list of series (`dataset_series`) and a window policy in
`datasets.window_policy`: either a fixed `{"start": iso, "end": iso}` or a relative
`{"last": "7d"}` resolved against the run's "now". Query-based selection arrives with the
connectors (sprint 9); `datasets.selection` stays `{}` until then.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.db.models import DEFAULT_ORG_ID, DEFAULT_WORKSPACE_ID, Dataset, DatasetSeries, Series
from tabayyun.services.pagination import decode_keyset, encode_keyset
from tabayyun.services.timeconv import parse_time

log = structlog.get_logger()

MIN_SERIES = 1
MAX_SERIES = 500
MIN_LAST = timedelta(hours=1)
MAX_LAST = timedelta(days=400)
_DURATION = re.compile(r"^(\d{1,6})(m|h|d|w)$")
_UNIT = {"m": timedelta(minutes=1), "h": timedelta(hours=1), "d": timedelta(days=1), "w": timedelta(weeks=1)}


class DatasetError(ValueError):
    """Invalid dataset input; `field` names the offending field for a 422 response."""

    def __init__(self, field: str, message: str) -> None:
        """Keep the field name and the message for the 422 response."""
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


@dataclass(frozen=True)
class SeriesRef:
    """A dataset member: series id and external id, for display."""

    id: uuid.UUID
    external_id: str


def parse_last(value: str) -> timedelta:
    """A `last` duration such as `90m`, `24h`, `7d` or `2w`, between 1 h and 400 d."""
    match = _DURATION.match(value.strip())
    if match is None:
        raise DatasetError("window", f"last must look like 24h, 7d or 2w, got {value!r}")
    span = int(match.group(1)) * _UNIT[match.group(2)]
    if not MIN_LAST <= span <= MAX_LAST:
        raise DatasetError("window", "last must be between 1h and 400d")
    return span


def window_policy(window: dict[str, Any]) -> dict[str, str]:
    """Validate a requested window and return the policy to store.

    Either `start` and `end` (RFC 3339 or epoch ns, start before end) or `last` alone.
    """
    keys = {k for k, v in window.items() if v is not None}
    if keys == {"last"}:
        last = str(window["last"]).strip()
        parse_last(last)
        return {"last": last}
    if keys == {"start", "end"}:
        try:
            start, end = parse_time(str(window["start"])), parse_time(str(window["end"]))
        except ValueError as exc:
            raise DatasetError("window", str(exc)) from exc
        if start >= end:
            raise DatasetError("window", "start must be before end")
        return {"start": start.isoformat(), "end": end.isoformat()}
    raise DatasetError("window", "give either start and end, or last")


def resolve_window(policy: dict[str, Any], now: datetime) -> tuple[datetime, datetime]:
    """The concrete `[start, end)` of a stored policy at `now`."""
    if "last" in policy:
        return now - parse_last(policy["last"]), now
    return parse_time(policy["start"]), parse_time(policy["end"])


async def _validated_series(session: AsyncSession, series_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    """1–500 distinct series that exist in the workspace."""
    n = len(series_ids)
    if not MIN_SERIES <= n <= MAX_SERIES:
        raise DatasetError("series_ids", f"needs {MIN_SERIES} to {MAX_SERIES} series, has {n}")
    if len(set(series_ids)) != n:
        dup = next(s for i, s in enumerate(series_ids) if s in series_ids[:i])
        raise DatasetError("series_ids", f"series {dup} is listed twice")
    found = set(
        (
            await session.execute(
                select(Series.id).where(
                    Series.workspace_id == DEFAULT_WORKSPACE_ID, Series.id.in_(series_ids)
                )
            )
        ).scalars()
    )
    missing = next((s for s in series_ids if s not in found), None)
    if missing is not None:
        raise DatasetError("series_ids", f"series {missing} not found")
    return list(series_ids)


async def _replace_series(
    session: AsyncSession, dataset_id: uuid.UUID, series_ids: Sequence[uuid.UUID]
) -> None:
    await session.execute(delete(DatasetSeries).where(DatasetSeries.dataset_id == dataset_id))
    session.add_all(DatasetSeries(dataset_id=dataset_id, series_id=s) for s in series_ids)


async def create_dataset(
    session: AsyncSession,
    *,
    name: str,
    series_ids: Sequence[uuid.UUID],
    window: dict[str, Any],
    now: datetime,
) -> Dataset:
    """Validate and store a dataset in the caller's transaction."""
    policy = window_policy(window)
    ids = await _validated_series(session, series_ids)
    dataset = Dataset(
        org_id=DEFAULT_ORG_ID,
        workspace_id=DEFAULT_WORKSPACE_ID,
        name=name,
        selection={},
        window_policy=policy,
        created_at=now,
    )
    session.add(dataset)
    await session.flush()
    await _replace_series(session, dataset.id, ids)
    await session.flush()
    log.info("dataset.created", dataset_id=str(dataset.id), n_series=len(ids), window=policy)
    return dataset


async def get_dataset(session: AsyncSession, dataset_id: uuid.UUID) -> Dataset | None:
    """One dataset of the workspace, or None."""
    dataset = await session.get(Dataset, dataset_id)
    return dataset if dataset is not None and dataset.workspace_id == DEFAULT_WORKSPACE_ID else None


async def series_of(
    session: AsyncSession, dataset_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[SeriesRef]]:
    """Member series of each dataset, ordered by external id."""
    rows = await session.execute(
        select(DatasetSeries.dataset_id, Series.id, Series.external_id)
        .join(Series, Series.id == DatasetSeries.series_id)
        .where(DatasetSeries.dataset_id.in_(dataset_ids))
        .order_by(DatasetSeries.dataset_id, Series.external_id)
    )
    out: dict[uuid.UUID, list[SeriesRef]] = {d: [] for d in dataset_ids}
    for dataset_id, series_id, external_id in rows:
        out[dataset_id].append(SeriesRef(series_id, external_id))
    return out


async def list_datasets(
    session: AsyncSession, *, limit: int, cursor: str | None
) -> tuple[list[Dataset], str | None]:
    """Datasets newest first with keyset pagination."""
    stmt = select(Dataset).where(Dataset.workspace_id == DEFAULT_WORKSPACE_ID)
    if cursor is not None:
        ts, last_id = decode_keyset(cursor)
        stmt = stmt.where((Dataset.created_at < ts) | ((Dataset.created_at == ts) & (Dataset.id < last_id)))
    rows = list(
        (
            await session.execute(
                stmt.order_by(Dataset.created_at.desc(), Dataset.id.desc()).limit(limit + 1)
            )
        ).scalars()
    )
    next_cursor = encode_keyset(rows[limit - 1].created_at, rows[limit - 1].id) if len(rows) > limit else None
    return rows[:limit], next_cursor


async def patch_dataset(
    session: AsyncSession, dataset_id: uuid.UUID, changes: dict[str, Any]
) -> Dataset | None:
    """Update any of name, series_ids and window; None when not found."""
    dataset = await get_dataset(session, dataset_id)
    if dataset is None:
        return None
    if "window" in changes:
        dataset.window_policy = window_policy(changes["window"])
    if "series_ids" in changes:
        await _replace_series(session, dataset.id, await _validated_series(session, changes["series_ids"]))
    if "name" in changes:
        dataset.name = changes["name"]
    await session.flush()
    return dataset


async def delete_dataset(session: AsyncSession, dataset_id: uuid.UUID) -> bool:
    """Delete a dataset and its series list; its runs stay with `dataset_id` null."""
    dataset = await get_dataset(session, dataset_id)
    if dataset is None:
        return False
    await session.delete(dataset)
    await session.flush()
    log.info("dataset.deleted", dataset_id=str(dataset_id))
    return True
