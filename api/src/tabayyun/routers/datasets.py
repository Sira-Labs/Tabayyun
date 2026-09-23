"""Dataset endpoints (spec 008): the series and window a dataset run covers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.db import get_session
from tabayyun.db.models import Dataset
from tabayyun.services import datasets as datasets_service

router = APIRouter(prefix="/api/datasets", tags=["datasets"])

Name = Annotated[str, Field(min_length=1, max_length=256)]


class WindowBody(BaseModel):
    """`{start, end}` (RFC 3339 or epoch ns) or `{last}` (e.g. `7d`)."""

    model_config = ConfigDict(extra="forbid")

    start: str | None = Field(default=None, max_length=64)
    end: str | None = Field(default=None, max_length=64)
    last: str | None = Field(default=None, max_length=16)


class DatasetCreate(BaseModel):
    """Body of `POST /api/datasets`."""

    model_config = ConfigDict(extra="forbid")

    name: Name
    series_ids: list[uuid.UUID]
    window: WindowBody


class DatasetPatch(BaseModel):
    """Body of `PATCH /api/datasets/{id}`: any of name, series_ids and window."""

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    series_ids: list[uuid.UUID] | None = None
    window: WindowBody | None = None


class SeriesRefOut(BaseModel):
    """A member series of a dataset."""

    id: str
    external_id: str


class DatasetOut(BaseModel):
    """A stored dataset: its series and window policy."""

    id: str
    name: str
    series: list[SeriesRefOut]
    window: dict[str, str]
    created_at: datetime


class DatasetList(BaseModel):
    """One page of datasets and the cursor of the next page, if any."""

    items: list[DatasetOut]
    next_cursor: str | None


def _dataset_out(dataset: Dataset, series: list[datasets_service.SeriesRef]) -> DatasetOut:
    return DatasetOut(
        id=str(dataset.id),
        name=dataset.name,
        series=[SeriesRefOut(id=str(s.id), external_id=s.external_id) for s in series],
        window=dataset.window_policy,
        created_at=dataset.created_at,
    )


async def _out(session: AsyncSession, dataset: Dataset) -> DatasetOut:
    series = await datasets_service.series_of(session, [dataset.id])
    return _dataset_out(dataset, series[dataset.id])


def _parse_id(dataset_id: str) -> uuid.UUID:
    """A malformed id is simply not found (404)."""
    try:
        return uuid.UUID(dataset_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="dataset not found") from exc


def _unprocessable(exc: datasets_service.DatasetError) -> HTTPException:
    return HTTPException(
        status_code=422, detail=[{"loc": ["body", exc.field], "msg": exc.message, "type": "value_error"}]
    )


@router.post("", response_model=DatasetOut, status_code=201)
async def create_dataset(
    body: Annotated[DatasetCreate, Body()], session: Annotated[AsyncSession, Depends(get_session)]
) -> DatasetOut:
    """Create a dataset; 422 names the invalid field."""
    try:
        dataset = await datasets_service.create_dataset(
            session,
            name=body.name,
            series_ids=body.series_ids,
            window=body.window.model_dump(),
            now=datetime.now(UTC),
        )
    except datasets_service.DatasetError as exc:
        raise _unprocessable(exc) from exc
    return await _out(session, dataset)


@router.get("", response_model=DatasetList)
async def list_datasets(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: str | None = None,
) -> DatasetList:
    """Datasets newest first."""
    try:
        rows, next_cursor = await datasets_service.list_datasets(session, limit=limit, cursor=cursor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    series = await datasets_service.series_of(session, [d.id for d in rows])
    return DatasetList(items=[_dataset_out(d, series[d.id]) for d in rows], next_cursor=next_cursor)


@router.get("/{dataset_id}", response_model=DatasetOut)
async def get_dataset(dataset_id: str, session: Annotated[AsyncSession, Depends(get_session)]) -> DatasetOut:
    """One dataset with its series."""
    dataset = await datasets_service.get_dataset(session, _parse_id(dataset_id))
    if dataset is None:
        raise HTTPException(status_code=404, detail="dataset not found")
    return await _out(session, dataset)


@router.patch("/{dataset_id}", response_model=DatasetOut)
async def patch_dataset(
    dataset_id: str,
    patch: Annotated[DatasetPatch, Body()],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DatasetOut:
    """Partial update of name, series and window."""
    changes: dict[str, Any] = {}
    for field in patch.model_fields_set:
        value = getattr(patch, field)
        if value is None:
            raise HTTPException(
                status_code=422,
                detail=[{"loc": ["body", field], "msg": "may not be null", "type": "value_error"}],
            )
        changes[field] = value.model_dump() if field == "window" else value
    try:
        dataset = await datasets_service.patch_dataset(session, _parse_id(dataset_id), changes)
    except datasets_service.DatasetError as exc:
        raise _unprocessable(exc) from exc
    if dataset is None:
        raise HTTPException(status_code=404, detail="dataset not found")
    return await _out(session, dataset)


@router.delete("/{dataset_id}", status_code=204)
async def delete_dataset(dataset_id: str, session: Annotated[AsyncSession, Depends(get_session)]) -> Response:
    """Delete a dataset; its past runs stay, with `dataset_id` cleared."""
    if not await datasets_service.delete_dataset(session, _parse_id(dataset_id)):
        raise HTTPException(status_code=404, detail="dataset not found")
    return Response(status_code=204)
