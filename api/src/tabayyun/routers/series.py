"""Series endpoints (spec 003): metric points and score history of one series.

Spec 004 adds reading and editing the series itself on this router.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.db import get_session
from tabayyun.services import findings as findings_service
from tabayyun.services.timeconv import datetime_to_ns, parse_time

router = APIRouter(prefix="/api/series", tags=["series"])


class MetricPoint(BaseModel):
    ts: int
    value: float
    run_id: str
    check_id: str
    name: str


class MetricList(BaseModel):
    items: list[MetricPoint]


class ScoreOut(BaseModel):
    series_id: str
    run_id: str
    layer: str
    method_version: str
    overall: float
    dimensions: dict[str, Any]
    n_findings: int
    computed_at: datetime


class ScoreList(BaseModel):
    items: list[ScoreOut]


async def _series_id_or_404(session: AsyncSession, series_id: str) -> uuid.UUID:
    try:
        parsed = uuid.UUID(series_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="series not found") from exc
    if await findings_service.get_series(session, parsed) is None:
        raise HTTPException(status_code=404, detail="series not found")
    return parsed


def _time_or_422(value: str | None, name: str) -> datetime | None:
    if value is None:
        return None
    try:
        return parse_time(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{name}: {exc}") from exc


@router.get("/{series_id}/metrics", response_model=MetricList)
async def list_metrics(
    series_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    name: Annotated[str | None, Query(max_length=128)] = None,
    since: str | None = None,
    until: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> MetricList:
    """Metric points newest first, optionally one metric name and a `[since, until)` range."""
    parsed = await _series_id_or_404(session, series_id)
    rows = await findings_service.list_metrics(
        session,
        parsed,
        name=name,
        since=_time_or_422(since, "since"),
        until=_time_or_422(until, "until"),
        limit=limit,
    )
    return MetricList(
        items=[
            MetricPoint(
                ts=datetime_to_ns(m.ts), value=m.value, run_id=str(m.run_id), check_id=m.check_id, name=m.name
            )
            for m in rows
        ]
    )


@router.get("/{series_id}/scores", response_model=ScoreList)
async def list_scores(
    series_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> ScoreList:
    """Score rows newest first."""
    parsed = await _series_id_or_404(session, series_id)
    rows = await findings_service.list_scores(session, parsed, limit=limit)
    return ScoreList(
        items=[
            ScoreOut(
                series_id=str(s.series_id),
                run_id=str(s.run_id),
                layer=s.layer,
                method_version=s.method_version,
                overall=s.overall,
                dimensions=s.dimensions,
                n_findings=s.n_findings,
                computed_at=s.computed_at,
            )
            for s in rows
        ]
    )
