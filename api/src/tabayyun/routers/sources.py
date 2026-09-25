"""Sources API (spec 004): list the workspace's sources. Creating sources arrives with the
connectors in sprint 9; until then the only source is `Uploads`, created on first upload."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.authz import ReadScope, get_session
from tabayyun.services import series as series_service

router = APIRouter(prefix="/api/sources", tags=["sources"])


class SourceOut(BaseModel):
    """One source and how many series it holds."""

    id: str
    type: str
    name: str
    n_series: int
    created_at: datetime


class SourceList(BaseModel):
    """All sources of the workspace, by name."""

    items: list[SourceOut]


@router.get("", response_model=SourceList)
async def list_sources(
    session: Annotated[AsyncSession, Depends(get_session)], scope: ReadScope
) -> SourceList:
    """The workspace's sources with their series counts."""
    rows = await series_service.list_sources(session, scope)
    return SourceList(
        items=[
            SourceOut(id=str(s.id), type=s.type, name=s.name, n_series=n, created_at=s.created_at)
            for s, n in rows
        ]
    )
