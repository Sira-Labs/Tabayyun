"""Runs API (spec 002): create a run from an upload, read one, list them.

`POST /api/runs` validates the form exactly like `/api/checks/run`, stores the raw bytes and
answers 202 at once; the checks run in the worker (or inline with `TABAYYUN_INLINE_JOBS`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun import core
from tabayyun.db import get_session
from tabayyun.services import runs as runs_service
from tabayyun.services import series as series_service
from tabayyun.services.runs import MAX_UPLOAD_BYTES, RunParams, UploadError

router = APIRouter(prefix="/api/runs", tags=["runs"])


class RunCreated(BaseModel):
    """Answer of `POST /api/runs`: the queued run."""

    id: str
    status: str
    created_at: datetime


class RunOut(BaseModel):
    """The `Run` response of spec 002."""

    id: str
    trigger: str
    status: str
    window: dict[str, int] | None
    now_ns: int | None
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    stats: dict[str, Any]
    series: list[dict[str, Any]]
    error: str | None
    created_at: datetime


class RunList(BaseModel):
    """One page of runs and the cursor of the next page, if any."""

    items: list[RunOut]
    next_cursor: str | None


@router.post("", status_code=202, response_model=RunCreated)
async def create_run(
    request: Request,
    background: BackgroundTasks,
    session: Annotated[AsyncSession, Depends(get_session)],
    file: Annotated[UploadFile, File(description="CSV with a timestamp column and a value column")],
    series_id: Annotated[str, Form(min_length=1, max_length=256)] = "uploaded",
    unit: Annotated[str | None, Form(max_length=32)] = None,
    ts_col: Annotated[str, Form(max_length=128)] = "ts",
    value_col: Annotated[str, Form(max_length=128)] = "value",
    quality_col: Annotated[str | None, Form(max_length=128)] = None,
    ingest_col: Annotated[str | None, Form(max_length=128)] = None,
    physical_min: Annotated[float | None, Form()] = None,
    physical_max: Annotated[float | None, Form()] = None,
    now_ns: Annotated[int | None, Form()] = None,
    ts_unit: Annotated[core.TsUnit, Form(description="Unit of epoch integer timestamps")] = "auto",
) -> RunCreated:
    """Store the upload, create a queued run and enqueue it; 202 with the run id."""
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    params = RunParams(
        series_id=series_id,
        unit=unit or None,
        ts_col=ts_col,
        value_col=value_col,
        quality_col=quality_col or None,
        ingest_col=ingest_col or None,
        physical_min=physical_min,
        physical_max=physical_max,
        now_ns=now_ns,
        ts_unit=ts_unit,
    )
    try:
        # Parse now so a bad file fails the request; the table is discarded, the bytes kept.
        runs_service.parse_upload(
            data,
            ts_col=ts_col,
            value_col=value_col,
            quality_col=params.quality_col,
            ingest_col=params.ingest_col,
            ts_unit=ts_unit,
        )
    except UploadError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    # The form's limits must fit the stored series (spec 004); checked again by the worker.
    stored = await series_service.find_upload_series(session, series_id)
    overrides = series_service.upload_overrides(params.unit, physical_min, physical_max)
    try:
        series_service.merged_for_run(stored, overrides)
    except series_service.MetadataError as exc:
        raise HTTPException(
            status_code=422,
            detail=[{"loc": ["body", exc.field], "msg": exc.message, "type": "value_error"}],
        ) from exc
    run = await runs_service.create_run(
        session,
        data=data,
        filename=file.filename or "upload.csv",
        content_type=file.content_type,
        params=params,
    )
    if request.app.state.settings.inline_jobs:
        # Background tasks may run before the session dependency commits; commit here so the
        # inline worker sees the run. The dependency's exit then finds nothing left to commit.
        await session.commit()
        background.add_task(
            runs_service.execute_run, request.app.state.session_factory, run.id, request.app.state.run_cache
        )
    else:
        await runs_service.enqueue_run(session, run.id)
    return RunCreated(id=str(run.id), status=run.status, created_at=run.created_at)


def _parse_id(run_id: str) -> uuid.UUID:
    """A malformed run id is simply not found (404)."""
    try:
        return uuid.UUID(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc


@router.get("/{run_id}", response_model=RunOut)
async def get_run(run_id: str, session: Annotated[AsyncSession, Depends(get_session)]) -> RunOut:
    """One run; 404 for unknown ids and ids outside the caller's workspace."""
    run = await runs_service.get_run(session, _parse_id(run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return RunOut(**runs_service.run_to_dict(run))


@router.get("", response_model=RunList)
async def list_runs(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> RunList:
    """Runs newest first with a keyset cursor."""
    try:
        runs, next_cursor = await runs_service.list_runs(session, limit=limit, cursor=cursor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RunList(items=[RunOut(**runs_service.run_to_dict(r)) for r in runs], next_cursor=next_cursor)
