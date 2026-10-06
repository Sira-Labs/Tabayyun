"""Sources API (spec 004, spec 021): the workspace's sources; connector sources are created,
changed, given credentials and series, checked and fetched here.

Roles: viewers read, editors register series and request fetches and checks, workspace
admins create and change sources and their credentials. Credentials are write-only.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun import connectors
from tabayyun.auth.deps import client_ip
from tabayyun.authz import ManageScope, Principal, ReadScope, WriteScope, get_principal, get_session
from tabayyun.authz.scope import Scope
from tabayyun.db import for_org
from tabayyun.db.models import SERIES_KINDS, Source, SourceFetch
from tabayyun.limits import limit
from tabayyun.services import fetches
from tabayyun.services import series as series_service
from tabayyun.services import sources as sources_service
from tabayyun.services.admin import audit

router = APIRouter(prefix="/api/sources", tags=["sources"])

SOURCE_WRITE_LIMIT = Depends(limit("source.write", "user"))
SOURCE_FETCH_LIMIT = Depends(limit("source.fetch", "user"))
SessionDep = Annotated[AsyncSession, Depends(get_session)]


class SourceOut(BaseModel):
    """One source and how many series it holds."""

    id: str
    type: str
    name: str
    n_series: int
    created_at: datetime
    enabled: bool
    connector: bool
    health_status: str


class SourceList(BaseModel):
    """All sources of the workspace, by name."""

    items: list[SourceOut]


class CredentialsState(BaseModel):
    """Whether credentials are stored, never what they are."""

    set: bool
    updated_at: datetime | None


class SourceDetail(BaseModel):
    """A source with its config, health and credentials state."""

    id: str
    type: str
    name: str
    enabled: bool
    connector: bool
    config: dict[str, Any]
    health: dict[str, Any]
    credentials: CredentialsState
    n_series: int
    polled_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SourceIn(BaseModel):
    """A new connector source."""

    type: str = Field(max_length=40)
    name: str = Field(min_length=1, max_length=200)
    config: dict[str, Any] = Field(default_factory=dict)


class SourcePatch(BaseModel):
    """Changes to a connector source; `config` replaces the whole config."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    config: dict[str, Any] | None = None
    enabled: bool | None = None


class PointIn(BaseModel):
    """A point of the source's system to make a series of."""

    external_id: str = Field(min_length=1, max_length=500)
    name: str = Field(min_length=1, max_length=200)
    unit: str | None = Field(default=None, max_length=50)
    kind: str = Field(default="measurement", pattern="^(" + "|".join(SERIES_KINDS) + ")$")


class SeriesRegistered(BaseModel):
    """How many points became series, and how many already were."""

    created: int
    existing: int


class FetchIn(BaseModel):
    """A window to fetch; all of the source's series unless `series_ids` names some."""

    start: datetime
    end: datetime
    series_ids: list[uuid.UUID] | None = Field(default=None, max_length=1000)
    force: bool = False


class FetchCreated(BaseModel):
    """The queued fetch."""

    id: str


class FetchOut(BaseModel):
    """One fetch of the history."""

    id: str
    trigger: str
    status: str
    window_start: datetime
    window_end: datetime
    series_ids: list[str] | None
    force: bool
    calls: int
    rows: int
    error: str | None
    requested_by: str | None
    run_id: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class FetchList(BaseModel):
    """The latest fetches, newest first."""

    items: list[FetchOut]


def _actor(request: Request, principal: Principal) -> audit.Actor:
    return audit.Actor(
        user_id=principal.user_id,
        org_id=principal.org_id,
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )


ActorDep = Annotated[Principal, Depends(get_principal)]


def _aware(value: datetime) -> datetime:
    """Naive datetimes are UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _detail(session: AsyncSession, source: Source) -> SourceDetail:
    v = await sources_service.view(session, source)
    return SourceDetail(
        id=str(source.id),
        type=source.type,
        name=source.name,
        enabled=source.enabled,
        connector=connectors.is_connector(source.type),
        config=source.config or {},
        health=source.health or {"status": "unknown"},
        credentials=CredentialsState(
            set=v.credentials_updated_at is not None, updated_at=v.credentials_updated_at
        ),
        n_series=v.n_series,
        polled_at=source.polled_at,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


def _fetch_out(f: SourceFetch) -> FetchOut:
    return FetchOut(
        id=str(f.id),
        trigger=f.trigger,
        status=f.status,
        window_start=f.window_start,
        window_end=f.window_end,
        series_ids=None if f.series_ids is None else [str(s) for s in f.series_ids],
        force=f.force,
        calls=f.calls,
        rows=f.rows,
        error=f.error,
        requested_by=None if f.requested_by is None else str(f.requested_by),
        run_id=None if f.run_id is None else str(f.run_id),
        created_at=f.created_at,
        started_at=f.started_at,
        finished_at=f.finished_at,
    )


async def _start(
    request: Request, background: BackgroundTasks, session: AsyncSession, scope: Scope, fetch: SourceFetch
) -> None:
    """Defer the fetch's job in this transaction, or run it after the response (inline jobs)."""
    state = request.app.state
    if not state.settings.inline_jobs:
        await fetches.enqueue(session, fetch)
        return
    await session.commit()
    deps = fetches.FetchDeps(cache=state.run_cache, net=state.net_policy, keyring=state.keyring)
    run = fetches.execute_check if fetch.trigger == "check" else fetches.execute_fetch
    background.add_task(run, for_org(state.session_factory, scope.org_id), deps, fetch.id)


@router.get("", response_model=SourceList)
async def list_sources(session: SessionDep, scope: ReadScope) -> SourceList:
    """The workspace's sources with their series counts."""
    rows = await series_service.list_sources(session, scope)
    return SourceList(
        items=[
            SourceOut(
                id=str(s.id),
                type=s.type,
                name=s.name,
                n_series=n,
                created_at=s.created_at,
                enabled=s.enabled,
                connector=connectors.is_connector(s.type),
                health_status=(s.health or {}).get("status", "unknown"),
            )
            for s, n in rows
        ]
    )


@router.post("", status_code=201, response_model=SourceDetail, dependencies=[SOURCE_WRITE_LIMIT])
async def create_source(
    request: Request, session: SessionDep, scope: ManageScope, principal: ActorDep, body: SourceIn
) -> SourceDetail:
    """A new connector source (workspace admins)."""
    source = await sources_service.create(
        session, scope, _actor(request, principal), source_type=body.type, name=body.name, config=body.config
    )
    return await _detail(session, source)


@router.get("/{source_id}", response_model=SourceDetail)
async def get_source(source_id: uuid.UUID, session: SessionDep, scope: ReadScope) -> SourceDetail:
    """One source: config, health, whether credentials are set, series count."""
    return await _detail(session, await sources_service.get_source(session, scope, source_id))


@router.patch("/{source_id}", response_model=SourceDetail, dependencies=[SOURCE_WRITE_LIMIT])
async def patch_source(
    source_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    scope: ManageScope,
    principal: ActorDep,
    body: SourcePatch,
) -> SourceDetail:
    """Rename, reconfigure, enable or disable a connector source."""
    source = await sources_service.update(
        session,
        scope,
        _actor(request, principal),
        source_id,
        name=body.name,
        config=body.config,
        enabled=body.enabled,
    )
    return await _detail(session, source)


@router.put("/{source_id}/credentials", status_code=204, dependencies=[SOURCE_WRITE_LIMIT])
async def put_credentials(
    source_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    scope: ManageScope,
    principal: ActorDep,
    body: Annotated[dict[str, Any], Body()],
) -> Response:
    """Store the source's credentials, encrypted; they are never returned."""
    await sources_service.set_credentials(
        session, scope, _actor(request, principal), source_id, body, request.app.state.keyring
    )
    return Response(status_code=204)


@router.delete("/{source_id}/credentials", status_code=204, dependencies=[SOURCE_WRITE_LIMIT])
async def delete_credentials(
    source_id: uuid.UUID, request: Request, session: SessionDep, scope: ManageScope, principal: ActorDep
) -> Response:
    """Forget the source's credentials."""
    await sources_service.clear_credentials(session, scope, _actor(request, principal), source_id)
    return Response(status_code=204)


@router.post("/{source_id}/series", response_model=SeriesRegistered)
async def register_series(
    source_id: uuid.UUID,
    session: SessionDep,
    scope: WriteScope,
    body: Annotated[list[PointIn], Body(max_length=sources_service.MAX_SERIES_PER_CALL)],
) -> SeriesRegistered:
    """Make points of the source's system series of the source (editors)."""
    created, existing = await sources_service.register_series(
        session, scope, source_id, [p.model_dump() for p in body]
    )
    return SeriesRegistered(created=created, existing=existing)


@router.post(
    "/{source_id}/check", status_code=202, response_model=FetchCreated, dependencies=[SOURCE_FETCH_LIMIT]
)
async def check_source(
    source_id: uuid.UUID,
    request: Request,
    background: BackgroundTasks,
    session: SessionDep,
    scope: WriteScope,
    principal: ActorDep,
) -> FetchCreated:
    """Reach the source's system in the worker; the outcome lands in its health."""
    now = datetime.now(UTC)
    fetch = await sources_service.request_fetch(
        session, scope, _actor(request, principal), source_id, trigger="check", start=now, end=now
    )
    await _start(request, background, session, scope, fetch)
    return FetchCreated(id=str(fetch.id))


@router.post(
    "/{source_id}/fetches", status_code=202, response_model=FetchCreated, dependencies=[SOURCE_FETCH_LIMIT]
)
async def request_fetch(
    source_id: uuid.UUID,
    request: Request,
    background: BackgroundTasks,
    session: SessionDep,
    scope: WriteScope,
    principal: ActorDep,
    body: FetchIn,
) -> FetchCreated:
    """Fetch a window of the source's series into the cache (what the cache lacks, or all with `force`)."""
    fetch = await sources_service.request_fetch(
        session,
        scope,
        _actor(request, principal),
        source_id,
        trigger="manual",
        start=_aware(body.start),
        end=_aware(body.end),
        series_ids=body.series_ids,
        force=body.force,
    )
    await _start(request, background, session, scope, fetch)
    return FetchCreated(id=str(fetch.id))


@router.get("/{source_id}/fetches", response_model=FetchList)
async def list_fetches(
    source_id: uuid.UUID,
    session: SessionDep,
    scope: ReadScope,
    limit_: Annotated[int, Query(alias="limit", ge=1, le=100)] = 20,
) -> FetchList:
    """The source's latest fetches, newest first."""
    rows = await sources_service.list_fetches(session, scope, source_id, limit_)
    return FetchList(items=[_fetch_out(f) for f in rows])
