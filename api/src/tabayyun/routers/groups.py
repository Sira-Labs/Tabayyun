"""Series group endpoints (spec 008): create, list, read, update and delete groups."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.db import get_session
from tabayyun.db.models import SeriesGroup
from tabayyun.services import groups as groups_service

router = APIRouter(prefix="/api/series-groups", tags=["series-groups"])

GroupKind = Literal["related", "redundant", "balance"]
MemberRole = Literal["member", "input", "output"]
Name = Annotated[str, Field(min_length=1, max_length=256)]


class MemberBody(BaseModel):
    """One member in a request: the series id and its role."""

    model_config = ConfigDict(extra="forbid")

    series_id: uuid.UUID
    role: MemberRole = "member"


class GroupCreate(BaseModel):
    """Body of `POST /api/series-groups`."""

    model_config = ConfigDict(extra="forbid")

    name: Name
    kind: GroupKind
    members: list[MemberBody]
    params: dict[str, Any] = Field(default_factory=dict)


class GroupPatch(BaseModel):
    """Body of `PATCH /api/series-groups/{id}`: any of name, members and params."""

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    members: list[MemberBody] | None = None
    params: dict[str, Any] | None = None


class MemberOut(BaseModel):
    """One member of a stored group."""

    series_id: str
    external_id: str
    role: str


class GroupOut(BaseModel):
    """A stored group with its members in declaration order."""

    id: str
    name: str
    kind: str
    members: list[MemberOut]
    params: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class GroupList(BaseModel):
    """One page of groups and the cursor of the next page, if any."""

    items: list[GroupOut]
    next_cursor: str | None


def _group_out(group: SeriesGroup, members: list[groups_service.MemberOut]) -> GroupOut:
    return GroupOut(
        id=str(group.id),
        name=group.name,
        kind=group.kind,
        members=[
            MemberOut(series_id=str(m.series_id), external_id=m.external_id, role=m.role) for m in members
        ],
        params=group.params,
        created_at=group.created_at,
        updated_at=group.updated_at,
    )


async def _out(session: AsyncSession, group: SeriesGroup) -> GroupOut:
    members = await groups_service.members_of(session, [group.id])
    return _group_out(group, members[group.id])


def _parse_id(group_id: str) -> uuid.UUID:
    """A malformed id is simply not found (404)."""
    try:
        return uuid.UUID(group_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="series group not found") from exc


def _unprocessable(exc: groups_service.GroupError) -> HTTPException:
    return HTTPException(
        status_code=422, detail=[{"loc": ["body", exc.field], "msg": exc.message, "type": "value_error"}]
    )


def _conflict() -> HTTPException:
    return HTTPException(status_code=409, detail="a series group with this name already exists")


def _members_in(members: list[MemberBody]) -> list[groups_service.MemberIn]:
    return [groups_service.MemberIn(series_id=m.series_id, role=m.role) for m in members]


@router.post("", response_model=GroupOut, status_code=201)
async def create_group(
    body: Annotated[GroupCreate, Body()], session: Annotated[AsyncSession, Depends(get_session)]
) -> GroupOut:
    """Create a group; 422 names the invalid field, 409 when the name is taken."""
    try:
        group = await groups_service.create_group(
            session,
            name=body.name,
            kind=body.kind,
            members=_members_in(body.members),
            params=body.params,
            now=datetime.now(UTC),
        )
    except groups_service.GroupError as exc:
        raise _unprocessable(exc) from exc
    except groups_service.GroupNameTakenError as exc:
        raise _conflict() from exc
    return await _out(session, group)


@router.get("", response_model=GroupList)
async def list_groups(
    session: Annotated[AsyncSession, Depends(get_session)],
    series_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: str | None = None,
) -> GroupList:
    """Groups newest first; `series_id` selects the groups a series belongs to."""
    parsed: uuid.UUID | None = None
    if series_id is not None:
        try:
            parsed = uuid.UUID(series_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="series_id is not a UUID") from exc
    try:
        rows, next_cursor = await groups_service.list_groups(
            session, series_id=parsed, limit=limit, cursor=cursor
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    members = await groups_service.members_of(session, [g.id for g in rows])
    return GroupList(items=[_group_out(g, members[g.id]) for g in rows], next_cursor=next_cursor)


@router.get("/{group_id}", response_model=GroupOut)
async def get_group(group_id: str, session: Annotated[AsyncSession, Depends(get_session)]) -> GroupOut:
    """One group with its members."""
    group = await groups_service.get_group(session, _parse_id(group_id))
    if group is None:
        raise HTTPException(status_code=404, detail="series group not found")
    return await _out(session, group)


@router.patch("/{group_id}", response_model=GroupOut)
async def patch_group(
    group_id: str,
    patch: Annotated[GroupPatch, Body()],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> GroupOut:
    """Partial update of name, members and params; members are validated against the kind."""
    changes: dict[str, Any] = {}
    for field in patch.model_fields_set:
        value = getattr(patch, field)
        if value is None:
            raise HTTPException(
                status_code=422,
                detail=[{"loc": ["body", field], "msg": "may not be null", "type": "value_error"}],
            )
        changes[field] = _members_in(value) if field == "members" else value
    try:
        group = await groups_service.patch_group(session, _parse_id(group_id), changes, now=datetime.now(UTC))
    except groups_service.GroupError as exc:
        raise _unprocessable(exc) from exc
    except groups_service.GroupNameTakenError as exc:
        raise _conflict() from exc
    if group is None:
        raise HTTPException(status_code=404, detail="series group not found")
    return await _out(session, group)


@router.delete("/{group_id}", status_code=204)
async def delete_group(group_id: str, session: Annotated[AsyncSession, Depends(get_session)]) -> Response:
    """Delete a group; findings it produced stay."""
    if not await groups_service.delete_group(session, _parse_id(group_id)):
        raise HTTPException(status_code=404, detail="series group not found")
    return Response(status_code=204)
