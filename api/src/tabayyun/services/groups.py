"""Series groups (spec 008): which series belong together and how.

A group is `related` (they usually move together), `redundant` (they measure the same
quantity) or a `balance` (inputs minus outputs closes within a loss band). The structural
rules match `SeriesGroup::validate` in the core; this module checks them with field names for
422 responses and also checks that every member exists in the workspace.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.authz.scope import Scope
from tabayyun.db.models import (
    Series,
    SeriesGroup,
    SeriesGroupMember,
)
from tabayyun.services.pagination import decode_keyset, encode_keyset

log = structlog.get_logger()

MIN_MEMBERS = 2
MAX_MEMBERS = 32
MAX_PARAMS_BYTES = 8 * 1024


class GroupError(ValueError):
    """Invalid group input; `field` names the offending field for a 422 response."""

    def __init__(self, field: str, message: str) -> None:
        """Keep the field name and the message for the 422 response."""
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


class GroupNameTakenError(Exception):
    """Another group in the workspace already has this name (409)."""


@dataclass(frozen=True)
class MemberIn:
    """One requested member: a series id and its role."""

    series_id: uuid.UUID
    role: str


@dataclass(frozen=True)
class MemberOut:
    """One stored member with the series' external id, for display."""

    series_id: uuid.UUID
    external_id: str
    role: str


def validate_members(kind: str, members: Sequence[MemberIn]) -> None:
    """Member count, duplicates and roles for a group kind (same rules as the core)."""
    n = len(members)
    if not MIN_MEMBERS <= n <= MAX_MEMBERS:
        raise GroupError("members", f"needs {MIN_MEMBERS} to {MAX_MEMBERS} members, has {n}")
    seen: set[uuid.UUID] = set()
    for m in members:
        if m.series_id in seen:
            raise GroupError("members", f"series {m.series_id} is listed twice")
        seen.add(m.series_id)
    roles = [m.role for m in members]
    if kind in ("related", "redundant") and any(r != "member" for r in roles):
        raise GroupError("members", f"{kind} groups take role `member` only")
    if kind == "balance":
        if "member" in roles:
            raise GroupError("members", "balance groups take roles `input` and `output` only")
        if "input" not in roles or "output" not in roles:
            raise GroupError("members", "balance groups need at least one input and one output")


def validate_params(params: dict[str, Any]) -> None:
    """Group params are a JSON object of at most 8 KiB, measured as compact UTF-8 JSON."""
    if len(json.dumps(params, separators=(",", ":"), ensure_ascii=False).encode()) > MAX_PARAMS_BYTES:
        raise GroupError("params", f"must be at most {MAX_PARAMS_BYTES} bytes as JSON")


async def _check_series_exist(session: AsyncSession, scope: Scope, members: Sequence[MemberIn]) -> None:
    """Every member series exists in the workspace; the first missing one is named."""
    ids = [m.series_id for m in members]
    found = set(
        (
            await session.execute(
                select(Series.id).where(Series.workspace_id == scope.workspace_id, Series.id.in_(ids))
            )
        ).scalars()
    )
    missing = next((i for i in ids if i not in found), None)
    if missing is not None:
        raise GroupError("members", f"series {missing} not found")


async def _name_taken(
    session: AsyncSession, scope: Scope, name: str, exclude: uuid.UUID | None = None
) -> bool:
    """Whether another group in the workspace has `name`."""
    stmt = select(SeriesGroup.id).where(
        SeriesGroup.workspace_id == scope.workspace_id, SeriesGroup.name == name
    )
    if exclude is not None:
        stmt = stmt.where(SeriesGroup.id != exclude)
    return (await session.execute(stmt.limit(1))).first() is not None


async def _replace_members(
    session: AsyncSession, scope: Scope, group_id: uuid.UUID, members: Sequence[MemberIn]
) -> None:
    """Store `members` in declaration order, replacing the previous list."""
    await session.execute(delete(SeriesGroupMember).where(SeriesGroupMember.group_id == group_id))
    session.add_all(
        SeriesGroupMember(
            group_id=group_id, org_id=scope.org_id, series_id=m.series_id, role=m.role, position=i
        )
        for i, m in enumerate(members)
    )


async def _flush_or_conflict(session: AsyncSession) -> None:
    """Flush; a unique violation on the name (a concurrent create) becomes a 409."""
    try:
        await session.flush()
    except IntegrityError as exc:
        if "uq_series_groups_workspace_id_name" in str(exc.orig):
            raise GroupNameTakenError from exc
        raise


async def create_group(
    session: AsyncSession,
    scope: Scope,
    *,
    name: str,
    kind: str,
    members: Sequence[MemberIn],
    params: dict[str, Any],
    now: datetime,
) -> SeriesGroup:
    """Validate and store a new group in the caller's transaction."""
    validate_members(kind, members)
    validate_params(params)
    await _check_series_exist(session, scope, members)
    if await _name_taken(session, scope, name):
        raise GroupNameTakenError
    group = SeriesGroup(
        org_id=scope.org_id,
        workspace_id=scope.workspace_id,
        name=name,
        kind=kind,
        params=params,
        created_at=now,
        updated_at=now,
    )
    session.add(group)
    await _flush_or_conflict(session)
    await _replace_members(session, scope, group.id, members)
    await session.flush()
    log.info("group.created", group_id=str(group.id), kind=kind, n_members=len(members))
    return group


async def get_group(session: AsyncSession, scope: Scope, group_id: uuid.UUID) -> SeriesGroup | None:
    """One group of the workspace, or None."""
    group = await session.get(SeriesGroup, group_id)
    return group if group is not None and group.workspace_id == scope.workspace_id else None


async def members_of(
    session: AsyncSession, group_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[MemberOut]]:
    """Members of each group in declaration order, with the series' external ids."""
    rows = await session.execute(
        select(SeriesGroupMember, Series.external_id)
        .join(Series, Series.id == SeriesGroupMember.series_id)
        .where(SeriesGroupMember.group_id.in_(group_ids))
        .order_by(SeriesGroupMember.group_id, SeriesGroupMember.position)
    )
    out: dict[uuid.UUID, list[MemberOut]] = {g: [] for g in group_ids}
    for member, external_id in rows:
        out[member.group_id].append(MemberOut(member.series_id, external_id, member.role))
    return out


async def list_groups(
    session: AsyncSession, scope: Scope, *, series_id: uuid.UUID | None, limit: int, cursor: str | None
) -> tuple[list[SeriesGroup], str | None]:
    """Groups newest first, optionally those containing `series_id`; keyset pagination."""
    stmt = select(SeriesGroup).where(SeriesGroup.workspace_id == scope.workspace_id)
    if series_id is not None:
        stmt = stmt.where(
            SeriesGroup.id.in_(
                select(SeriesGroupMember.group_id).where(SeriesGroupMember.series_id == series_id)
            )
        )
    if cursor is not None:
        ts, last_id = decode_keyset(cursor)
        stmt = stmt.where(
            (SeriesGroup.created_at < ts) | ((SeriesGroup.created_at == ts) & (SeriesGroup.id < last_id))
        )
    rows = list(
        (
            await session.execute(
                stmt.order_by(SeriesGroup.created_at.desc(), SeriesGroup.id.desc()).limit(limit + 1)
            )
        ).scalars()
    )
    next_cursor = encode_keyset(rows[limit - 1].created_at, rows[limit - 1].id) if len(rows) > limit else None
    return rows[:limit], next_cursor


async def patch_group(
    session: AsyncSession, scope: Scope, group_id: uuid.UUID, changes: dict[str, Any], *, now: datetime
) -> SeriesGroup | None:
    """Update any of name, members and params; the kind is fixed. None when not found."""
    group = await get_group(session, scope, group_id)
    if group is None:
        return None
    if "name" in changes and changes["name"] != group.name:
        if await _name_taken(session, scope, changes["name"], exclude=group.id):
            raise GroupNameTakenError
        group.name = changes["name"]
    if "params" in changes:
        validate_params(changes["params"])
        group.params = changes["params"]
    if "members" in changes:
        members: list[MemberIn] = changes["members"]
        validate_members(group.kind, members)
        await _check_series_exist(session, scope, members)
        await _replace_members(session, scope, group.id, members)
    group.updated_at = now
    await _flush_or_conflict(session)
    return group


async def delete_group(session: AsyncSession, scope: Scope, group_id: uuid.UUID) -> bool:
    """Delete a group and its members; False when not found."""
    group = await get_group(session, scope, group_id)
    if group is None:
        return False
    await session.delete(group)
    await session.flush()
    log.info("group.deleted", group_id=str(group_id))
    return True


async def groups_within(
    session: AsyncSession, scope: Scope, series_ids: Sequence[uuid.UUID]
) -> list[SeriesGroup]:
    """Groups of the workspace that have at least one member among `series_ids`."""
    rows = await session.execute(
        select(SeriesGroup)
        .where(
            SeriesGroup.workspace_id == scope.workspace_id,
            SeriesGroup.id.in_(
                select(SeriesGroupMember.group_id).where(SeriesGroupMember.series_id.in_(series_ids))
            ),
        )
        .order_by(SeriesGroup.name)
    )
    return list(rows.scalars())
