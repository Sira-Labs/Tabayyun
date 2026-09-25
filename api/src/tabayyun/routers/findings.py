"""Findings API (spec 003): list with filters, read one, change its status.

Lifecycle and deduplication rules are in ADR-0013. Deleting is not offered: a wrong finding
is resolved with a reason.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.authz import ReadScope, WriteScope, get_session
from tabayyun.db.models import FINDING_STATUSES
from tabayyun.services import findings as findings_service
from tabayyun.services.findings import DIMENSIONS, MAX_REASON_CHARS, SEVERITIES, FindingFilter
from tabayyun.services.timeconv import parse_time

router = APIRouter(prefix="/api/findings", tags=["findings"])

StatusName = Literal["open", "acked", "muted", "resolved"]


class FindingOut(BaseModel):
    """The `Finding` response of spec 003; `window` bounds are ns since the epoch."""

    id: str
    check_id: str
    series_id: str
    dimension: str
    severity: str
    window: dict[str, int]
    score_impact: float
    summary: str
    evidence: dict[str, Any]
    status: str
    status_reason: str | None
    status_at: datetime | None
    first_run_id: str | None
    last_run_id: str | None
    occurrences: int
    created_at: datetime
    updated_at: datetime


class FindingList(BaseModel):
    """One page of findings and the cursor of the next page, if any."""

    items: list[FindingOut]
    next_cursor: str | None


class StatusChange(BaseModel):
    """Body of `PATCH /api/findings/{id}`; muting needs a reason."""

    status: StatusName
    reason: str | None = Field(default=None, max_length=MAX_REASON_CHARS)

    @model_validator(mode="after")
    def _muting_needs_reason(self) -> StatusChange:
        """Reject `muted` without a non-blank reason."""
        if self.status == "muted" and not (self.reason or "").strip():
            raise ValueError("muting a finding needs a non-empty reason")
        return self


def _uuid_or_422(value: str | None, name: str) -> uuid.UUID | None:
    """Parse an optional UUID query parameter; 422 when malformed."""
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{name} is not a UUID") from exc


def _choices(value: str | None, allowed: tuple[str, ...], name: str) -> tuple[str, ...] | None:
    """Comma list restricted to `allowed`; None when absent."""
    if value is None:
        return None
    items = tuple(dict.fromkeys(v.strip() for v in value.split(",") if v.strip()))
    unknown = [v for v in items if v not in allowed]
    if not items or unknown:
        raise HTTPException(
            status_code=422, detail=f"{name} must be a comma list of {', '.join(allowed)}; got {value!r}"
        )
    return items


def _time_or_422(value: str | None, name: str) -> datetime | None:
    """Parse an optional RFC 3339 or epoch-ns query parameter; 422 when malformed."""
    if value is None:
        return None
    try:
        return parse_time(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{name}: {exc}") from exc


def _statuses(value: str | None) -> tuple[str, ...] | None:
    """Default open and acked; `all` means every status."""
    if value is None:
        return findings_service.DEFAULT_LIST_STATUSES
    if value.strip() == "all":
        return None
    return _choices(value, FINDING_STATUSES, "status")


@router.get("", response_model=FindingList)
async def list_findings(
    session: Annotated[AsyncSession, Depends(get_session)],
    scope: ReadScope,
    series_id: str | None = None,
    check_id: Annotated[str | None, Query(max_length=128)] = None,
    severity: str | None = None,
    dimension: str | None = None,
    status: str | None = None,
    run_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    cursor: str | None = None,
) -> FindingList:
    """Findings whose window overlaps `[since, until)`, newest window first."""
    if dimension is not None and dimension not in DIMENSIONS:
        raise HTTPException(status_code=422, detail=f"dimension must be one of {', '.join(DIMENSIONS)}")
    filters = FindingFilter(
        series_id=_uuid_or_422(series_id, "series_id"),
        check_id=check_id,
        severities=_choices(severity, SEVERITIES, "severity"),
        dimension=dimension,
        statuses=_statuses(status),
        run_id=_uuid_or_422(run_id, "run_id"),
        since=_time_or_422(since, "since"),
        until=_time_or_422(until, "until"),
    )
    try:
        rows, next_cursor = await findings_service.list_findings(
            session, scope, filters=filters, limit=limit, cursor=cursor
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return FindingList(
        items=[FindingOut(**findings_service.finding_to_dict(f)) for f in rows], next_cursor=next_cursor
    )


def _finding_id(finding_id: str) -> uuid.UUID:
    """Parse a finding id from the path; a malformed id is simply not found (404)."""
    try:
        return uuid.UUID(finding_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="finding not found") from exc


@router.get("/{finding_id}", response_model=FindingOut)
async def get_finding(
    finding_id: str, session: Annotated[AsyncSession, Depends(get_session)], scope: ReadScope
) -> FindingOut:
    """One finding; 404 for unknown ids."""
    finding = await findings_service.get_finding(session, scope, _finding_id(finding_id))
    if finding is None:
        raise HTTPException(status_code=404, detail="finding not found")
    return FindingOut(**findings_service.finding_to_dict(finding))


@router.patch("/{finding_id}", response_model=FindingOut)
async def change_status(
    finding_id: str,
    change: Annotated[StatusChange, Body()],
    session: Annotated[AsyncSession, Depends(get_session)],
    scope: WriteScope,
) -> FindingOut:
    """Move a finding through its lifecycle; 409 with the current status on a forbidden move."""
    try:
        finding = await findings_service.change_status(
            session,
            scope,
            _finding_id(finding_id),
            status=change.status,
            reason=change.reason,
            now=datetime.now(UTC),
        )
    except findings_service.FindingNotFoundError as exc:
        raise HTTPException(status_code=404, detail="finding not found") from exc
    except findings_service.TransitionError as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc), "status": exc.current}) from exc
    return FindingOut(**findings_service.finding_to_dict(finding))
