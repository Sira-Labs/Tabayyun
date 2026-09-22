"""Check execution endpoints (stateless, sprint 2).

`POST /api/checks/run` accepts a CSV upload and runs the built-in checks on it. Persistence,
scheduling and authorization arrive with the workspace model.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from tabayyun import core
from tabayyun.services.runs import MAX_UPLOAD_BYTES, UploadError, parse_upload

router = APIRouter(prefix="/api/checks", tags=["checks"])


@router.get("")
async def list_checks() -> list[str]:
    return core.builtin_checks()


@router.post("/run", response_model=core.CheckReport)
async def run_checks(
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
) -> core.CheckReport:
    """Run the built-in checks on an uploaded CSV without storing anything."""
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    try:
        table = parse_upload(
            data,
            ts_col=ts_col,
            value_col=value_col,
            quality_col=quality_col or None,
            ingest_col=ingest_col or None,
            ts_unit=ts_unit,
        ).table
    except UploadError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    meta = core.SeriesMetaIn(
        id=series_id, unit=unit or None, physical_min=physical_min, physical_max=physical_max
    )
    try:
        return core.run_checks(
            table,
            meta,
            now_ns=now_ns,
            ts_col=ts_col,
            value_col=value_col,
            quality_col=quality_col or None,
            ingest_col=ingest_col or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
