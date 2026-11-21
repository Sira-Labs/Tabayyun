"""`GET /api/overview`: the workspace at a glance (spec 032)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.authz import ReadScope, get_session
from tabayyun.services import overview as overview_service

router = APIRouter(prefix="/api/overview", tags=["overview"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _refuse(field: str, message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=[{"field": field, "message": message}])


@router.get("")
async def get_overview(
    session: SessionDep,
    scope: ReadScope,
    days: Annotated[int, Query()] = overview_service.DEFAULT_WINDOW,
    asset: Annotated[str | None, Query()] = None,
    source: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Score tiles with their trend, the worst series, new findings per day and the sources,
    for the last 7, 30 or 90 days, optionally for one asset path prefix or one source."""
    if days not in overview_service.WINDOWS:
        raise _refuse("days", f"must be one of {', '.join(map(str, overview_service.WINDOWS))}")
    prefix = (asset or "").strip() or None
    if prefix is not None and len(prefix) > overview_service.MAX_ASSET_PREFIX:
        raise _refuse("asset", f"must have at most {overview_service.MAX_ASSET_PREFIX} characters")
    source_id = None
    if source:
        try:
            source_id = uuid.UUID(source)
        except ValueError as exc:
            raise _refuse("source", "must be a source id") from exc
    filters = overview_service.Filters(days=days, asset=prefix, source=source_id)
    return await overview_service.overview(session, scope, filters, today=datetime.now(UTC).date())
