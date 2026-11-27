"""Correction endpoints (spec 035): propose a repair of a window of a series, list and read
corrections, approve or reject one.

Reading needs any role in the workspace; proposing and deciding need its editor role.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.auth.deps import client_ip
from tabayyun.authz import Principal, ReadScope, Scope, WriteScope, get_principal, get_session
from tabayyun.db import for_org
from tabayyun.db.models import Correction, Series
from tabayyun.services import corrections as service
from tabayyun.services import series as series_service
from tabayyun.services.admin import audit

router = APIRouter(prefix="/api", tags=["corrections"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
ActorDep = Annotated[Principal, Depends(get_principal)]


class CorrectionCreate(BaseModel):
    """Body of `POST /api/series/{id}/corrections`; `from` and `to` are ns, as strings or integers."""

    model_config = ConfigDict(extra="forbid")

    method: str = Field(min_length=1, max_length=service.MAX_METHOD)
    params: dict[str, Any] = {}
    from_: int | str = Field(alias="from")
    to: int | str
    reason: str
    finding_id: str | None = None


class Decision(BaseModel):
    """Body of the approve and reject routes."""

    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=service.MAX_REASON)


def _actor(request: Request, principal: Principal) -> audit.Actor:
    return audit.Actor(
        user_id=principal.user_id,
        org_id=principal.org_id,
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )


def _uuid_or_404(value: str) -> uuid.UUID:
    """A malformed id is simply not found."""
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="not found") from exc


async def _series(session: AsyncSession, scope: Scope, series_id: str) -> Series:
    """The workspace's series, or 404 (also for a malformed id)."""
    series = await series_service.get_series(session, scope, _uuid_or_404(series_id))
    if series is None:
        raise HTTPException(status_code=404, detail="series not found")
    return series


async def _correction(session: AsyncSession, scope: Scope, correction_id: str) -> tuple[Correction, Series]:
    """The workspace's correction and its series, or 404 (also for a malformed id)."""
    correction = await service.get_correction(session, scope, _uuid_or_404(correction_id))
    series = (
        None if correction is None else await series_service.get_series(session, scope, correction.series_id)
    )
    if correction is None or series is None:
        raise HTTPException(status_code=404, detail="not found")
    return correction, series


async def _out(session: AsyncSession, correction: Correction, *, ranges: bool = True) -> dict[str, Any]:
    names = await service.user_names(session, [correction])
    found = await service.list_ranges(session, correction.id) if ranges else None
    return service.to_dict(correction, names, found)


def _finding_id(value: str | None) -> uuid.UUID | None:
    """The proposal's finding id; one that is not a UUID names no finding (422)."""
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=service.FindingNotFoundError.code) from exc


@router.post("/series/{series_id}/corrections", status_code=201)
async def propose_correction(
    series_id: str,
    body: CorrectionCreate,
    request: Request,
    session: SessionDep,
    principal: ActorDep,
    scope: WriteScope,
) -> dict[str, Any]:
    """Propose a correction and queue its computation; with inline jobs it is computed before
    the answer. 422 with the core's message for a refused operation, 422 `finding_not_found`."""
    series = await _series(session, scope, series_id)
    state = request.app.state
    inline = bool(state.settings.inline_jobs)
    try:
        proposal = service.validate(
            series,
            method=body.method,
            params=body.params,
            from_=body.from_,
            to=body.to,
            reason=body.reason,
            finding_id=_finding_id(body.finding_id),
        )
        correction = await service.propose(
            session, series, proposal, user_id=principal.user_id, defer=not inline
        )
    except service.FindingNotFoundError as exc:
        raise HTTPException(status_code=422, detail=exc.code) from exc
    except service.InvalidCorrectionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await audit.record(
        session,
        _actor(request, principal),
        "correction.proposed",
        "correction",
        correction.id,
        workspace_id=scope.workspace_id,
        details=service.audit_details(correction, series),
    )
    if inline:
        # The computation runs in its own transactions and must see the proposal.
        await session.commit()
        await service.compute(
            for_org(state.session_factory, scope.org_id),
            correction.id,
            state.run_cache,
            state.settings.correction_max_rows,
        )
        # The request's transaction is over: read the outcome in a new one.
        async with for_org(state.session_factory, scope.org_id)() as fresh, fresh.begin():
            computed = await fresh.get(Correction, correction.id)
            return await _out(fresh, computed or correction, ranges=False)
    return await _out(session, correction, ranges=False)


@router.get("/series/{series_id}/corrections")
async def list_corrections(
    series_id: str,
    session: SessionDep,
    scope: ReadScope,
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    """The series' corrections, newest version first; `status` keeps one status."""
    series = await _series(session, scope, series_id)
    try:
        rows, next_cursor = await service.list_corrections(
            session, series.id, status=status or None, limit=limit, cursor=cursor
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    names = await service.user_names(session, rows)
    return {"items": [service.to_dict(c, names) for c in rows], "next_cursor": next_cursor}


@router.get("/corrections/{correction_id}")
async def get_correction(correction_id: str, session: SessionDep, scope: ReadScope) -> dict[str, Any]:
    """One correction with the ranges it touched."""
    correction, _series_row = await _correction(session, scope, correction_id)
    return await _out(session, correction)


async def _decide(
    action: str,
    correction_id: str,
    body: Decision | None,
    request: Request,
    session: AsyncSession,
    principal: Principal,
    scope: Scope,
) -> dict[str, Any]:
    """Approve or reject, with the audit event; a refusal is 409 with its code."""
    correction, series = await _correction(session, scope, correction_id)
    decide = service.approve if action == "approved" else service.reject
    try:
        await decide(session, correction, user_id=principal.user_id, note=body.note if body else None)
    except service.CorrectionError as exc:
        raise HTTPException(status_code=409, detail=exc.code) from exc
    await audit.record(
        session,
        _actor(request, principal),
        f"correction.{action}",
        "correction",
        correction.id,
        workspace_id=scope.workspace_id,
        details=service.audit_details(correction, series),
    )
    return await _out(session, correction)


@router.post("/corrections/{correction_id}/approve")
async def approve_correction(
    correction_id: str,
    request: Request,
    session: SessionDep,
    principal: ActorDep,
    scope: WriteScope,
    body: Decision | None = None,
) -> dict[str, Any]:
    """`computed` → `approved`. 409 `not_computed` in any other status, 409 `stale` when an
    overlapping correction was approved after this one was computed."""
    return await _decide("approved", correction_id, body, request, session, principal, scope)


@router.post("/corrections/{correction_id}/reject")
async def reject_correction(
    correction_id: str,
    request: Request,
    session: SessionDep,
    principal: ActorDep,
    scope: WriteScope,
    body: Decision | None = None,
) -> dict[str, Any]:
    """`proposed` or `computed` → `rejected`. 409 `already_decided` in any other status."""
    return await _decide("rejected", correction_id, body, request, session, principal, scope)
