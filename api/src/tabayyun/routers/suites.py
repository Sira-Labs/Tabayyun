"""Check suite endpoints (spec 027): suites, their thresholds, and starting a run now."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.auth.deps import client_ip
from tabayyun.authz import Principal, ReadScope, WriteScope, get_principal, get_session
from tabayyun.db.models import CheckSuite, Run
from tabayyun.routers.runs import RunCreated, dispatch_dataset_run
from tabayyun.services import suites as suites_service
from tabayyun.services.admin import audit
from tabayyun.services.datasets import DatasetError

router = APIRouter(prefix="/api/suites", tags=["suites"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
ActorDep = Annotated[Principal, Depends(get_principal)]


class CheckBody(BaseModel):
    """One check of a suite in a request."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)


class SuiteCreate(BaseModel):
    """Body of `POST /api/suites`."""

    model_config = ConfigDict(extra="forbid")

    name: str
    dataset_id: uuid.UUID
    schedule: str | None = None
    enabled: bool = True
    checks: list[CheckBody] | None = None


class SuitePatch(BaseModel):
    """Body of `PATCH /api/suites/{id}`: any of the fields; `schedule: null` removes it."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    dataset_id: uuid.UUID | None = None
    schedule: str | None = None
    enabled: bool | None = None
    checks: list[CheckBody] | None = None


class ThresholdOut(BaseModel):
    """A parameter of a check with the value that applies and where it comes from."""

    param: str
    value: Any
    source: str = Field(description="override, default, metadata or baseline")
    note: str | None


class SuiteCheckOut(BaseModel):
    """One check of a stored suite."""

    id: str
    scope: str
    enabled: bool
    params: dict[str, Any]
    thresholds: list[ThresholdOut]


class LastRunOut(BaseModel):
    """The newest run of a suite."""

    id: str
    status: str
    created_at: datetime


class SuiteOut(BaseModel):
    """A suite as the API returns it."""

    id: str
    name: str
    dataset_id: str | None
    dataset_name: str | None
    schedule: str | None
    enabled: bool
    checks: list[SuiteCheckOut]
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_run: LastRunOut | None
    created_at: datetime
    updated_at: datetime


class SuiteList(BaseModel):
    """One page of suites and the cursor of the next page, if any."""

    items: list[SuiteOut]
    next_cursor: str | None


def _actor(request: Request, principal: Principal) -> audit.Actor:
    return audit.Actor(
        user_id=principal.user_id,
        org_id=principal.org_id,
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )


def _parse_id(suite_id: str) -> uuid.UUID:
    """A malformed id is simply not found (404)."""
    try:
        return uuid.UUID(suite_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="suite not found") from exc


def _unprocessable(exc: suites_service.SuiteError) -> HTTPException:
    return HTTPException(
        status_code=422, detail=[{"loc": ["body", exc.field], "msg": exc.message, "type": "value_error"}]
    )


def _conflict() -> HTTPException:
    return HTTPException(status_code=409, detail="a suite with this name already exists")


def _suite_out(suite: CheckSuite, dataset_name: str | None, last_run: Run | None) -> SuiteOut:
    known = suites_service.catalogue()
    checks = []
    for check in suite.checks:
        info = known.get(check["id"])
        if info is None:  # a check a later version no longer ships
            continue
        thresholds = [
            ThresholdOut(param=t.param, value=t.value, source=t.source, note=t.note)
            for t in suites_service.thresholds(check, info)
        ]
        checks.append(
            SuiteCheckOut(
                id=check["id"],
                scope=info["scope"],
                enabled=bool(check.get("enabled", True)),
                params=check.get("params") or {},
                thresholds=thresholds,
            )
        )
    return SuiteOut(
        id=str(suite.id),
        name=suite.name,
        dataset_id=None if suite.dataset_id is None else str(suite.dataset_id),
        dataset_name=dataset_name,
        schedule=suite.schedule,
        enabled=suite.enabled,
        checks=checks,
        next_run_at=suite.next_run_at,
        last_run_at=suite.last_run_at,
        last_run=None
        if last_run is None
        else LastRunOut(id=str(last_run.id), status=last_run.status, created_at=last_run.created_at),
        created_at=suite.created_at,
        updated_at=suite.updated_at,
    )


async def _out(session: AsyncSession, suites: list[CheckSuite]) -> list[SuiteOut]:
    names = await suites_service.dataset_names(
        session, [s.dataset_id for s in suites if s.dataset_id is not None]
    )
    last = await suites_service.last_runs(session, [s.id for s in suites])
    return [_suite_out(s, names.get(s.dataset_id) if s.dataset_id else None, last.get(s.id)) for s in suites]


async def _suite_or_404(session: AsyncSession, scope: Any, suite_id: str) -> CheckSuite:
    suite = await suites_service.get_suite(session, scope, _parse_id(suite_id))
    if suite is None:
        raise HTTPException(status_code=404, detail="suite not found")
    return suite


def _checks(checks: list[CheckBody] | None) -> list[dict[str, Any]] | None:
    return None if checks is None else [c.model_dump() for c in checks]


@router.post("", response_model=SuiteOut, status_code=201)
async def create_suite(
    request: Request, session: SessionDep, scope: WriteScope, principal: ActorDep, body: SuiteCreate
) -> SuiteOut:
    """Create a suite. Without `checks` it runs every built-in check with its defaults."""
    try:
        suite = await suites_service.create_suite(
            session,
            scope,
            name=body.name,
            dataset_id=body.dataset_id,
            schedule=body.schedule,
            enabled=body.enabled,
            checks=_checks(body.checks),
            now=datetime.now(UTC),
        )
    except suites_service.SuiteError as exc:
        raise _unprocessable(exc) from exc
    except suites_service.SuiteNameTakenError as exc:
        raise _conflict() from exc
    await audit.record(
        session,
        _actor(request, principal),
        "suite.created",
        "suite",
        suite.id,
        workspace_id=scope.workspace_id,
        details={"name": suite.name, "schedule": suite.schedule},
    )
    return (await _out(session, [suite]))[0]


@router.get("", response_model=SuiteList)
async def list_suites(
    session: SessionDep,
    scope: ReadScope,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    cursor: str | None = None,
) -> SuiteList:
    """Suites newest first."""
    try:
        rows, next_cursor = await suites_service.list_suites(session, scope, limit=limit, cursor=cursor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return SuiteList(items=await _out(session, rows), next_cursor=next_cursor)


@router.get("/{suite_id}", response_model=SuiteOut)
async def get_suite(suite_id: str, session: SessionDep, scope: ReadScope) -> SuiteOut:
    """One suite with its checks and thresholds."""
    return (await _out(session, [await _suite_or_404(session, scope, suite_id)]))[0]


@router.patch("/{suite_id}", response_model=SuiteOut)
async def patch_suite(
    suite_id: str,
    request: Request,
    session: SessionDep,
    scope: WriteScope,
    principal: ActorDep,
    body: SuitePatch,
) -> SuiteOut:
    """Change any of name, dataset, schedule, enabled and checks."""
    suite = await _suite_or_404(session, scope, suite_id)
    changes = body.model_dump(exclude_unset=True)
    for field in ("name", "dataset_id", "enabled", "checks"):
        if field in changes and changes[field] is None:
            raise _unprocessable(suites_service.SuiteError(field, "must not be null"))
    try:
        changed = await suites_service.patch_suite(session, scope, suite, changes, now=datetime.now(UTC))
    except suites_service.SuiteError as exc:
        raise _unprocessable(exc) from exc
    except suites_service.SuiteNameTakenError as exc:
        raise _conflict() from exc
    if changed:
        await audit.record(
            session,
            _actor(request, principal),
            "suite.updated",
            "suite",
            suite.id,
            workspace_id=scope.workspace_id,
            details=changed,
        )
    return (await _out(session, [suite]))[0]


@router.delete("/{suite_id}", status_code=204)
async def delete_suite(
    suite_id: str, request: Request, session: SessionDep, scope: WriteScope, principal: ActorDep
) -> Response:
    """Delete a suite. Its runs stay."""
    suite = await _suite_or_404(session, scope, suite_id)
    name = suite.name
    await suites_service.delete_suite(session, suite)
    await audit.record(
        session,
        _actor(request, principal),
        "suite.deleted",
        "suite",
        suite.id,
        workspace_id=scope.workspace_id,
        details={"name": name},
    )
    return Response(status_code=204)


@router.post("/{suite_id}/runs", response_model=RunCreated, status_code=202)
async def run_suite_now(
    suite_id: str,
    request: Request,
    background: BackgroundTasks,
    session: SessionDep,
    scope: WriteScope,
    principal: ActorDep,
) -> RunCreated:
    """Start a run of the suite now, whatever its schedule. 409 `no_dataset` without a dataset."""
    suite = await _suite_or_404(session, scope, suite_id)
    try:
        run = await suites_service.start_run(session, scope, suite, now=datetime.now(UTC))
    except suites_service.NoDatasetError as exc:
        raise HTTPException(status_code=409, detail="no_dataset") from exc
    except DatasetError as exc:  # the dataset's window lies outside the core's range
        raise HTTPException(status_code=422, detail=exc.message) from exc
    await audit.record(
        session,
        _actor(request, principal),
        "suite.run_started",
        "suite",
        suite.id,
        workspace_id=scope.workspace_id,
        details={"run_id": str(run.id)},
    )
    return await dispatch_dataset_run(request, background, session, scope, run)
