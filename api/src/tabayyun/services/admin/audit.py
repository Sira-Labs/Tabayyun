"""The append-only audit log (spec 014): one event per mutation, written in the same transaction.

The app login may only insert and read `audit_events` (migration 0006). Reads are keyset-paged
by (`created_at`, `id`), newest first.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.db.models import AuditEvent, User


@dataclass(frozen=True)
class Actor:
    """Who makes a change, in which org, from where (IP and user agent are informational)."""

    user_id: uuid.UUID
    org_id: uuid.UUID
    ip_address: str | None = None
    user_agent: str | None = None


def change(before: Any, after: Any) -> dict[str, Any]:
    """The `details` shape of an update."""
    return {"before": before, "after": after}


async def record(
    session: AsyncSession,
    actor: Actor,
    action: str,
    target_type: str,
    target_id: uuid.UUID | str,
    *,
    workspace_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Add one event to the caller's transaction."""
    session.add(
        AuditEvent(
            id=uuid.uuid4(),
            org_id=actor.org_id,
            workspace_id=workspace_id,
            actor_user_id=actor.user_id,
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            details=details or {},
            ip_address=actor.ip_address,
            user_agent=(actor.user_agent or None) and actor.user_agent[:500],
        )
    )


@dataclass(frozen=True)
class AuditQuery:
    """Filters of the audit route. `workspace_ids` None means every event of the org."""

    workspace_ids: list[uuid.UUID] | None = None
    action_prefix: str | None = None
    actor_user_id: uuid.UUID | None = None
    before: tuple[datetime, uuid.UUID] | None = None
    limit: int = 50


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _filtered(query: AuditQuery) -> Select[tuple[AuditEvent, User]]:
    stmt = select(AuditEvent, User).outerjoin(User, User.id == AuditEvent.actor_user_id)
    if query.workspace_ids is not None:
        stmt = stmt.where(AuditEvent.workspace_id.in_(query.workspace_ids))
    if query.action_prefix:
        stmt = stmt.where(AuditEvent.action.like(_escape_like(query.action_prefix) + "%", escape="\\"))
    if query.actor_user_id is not None:
        stmt = stmt.where(AuditEvent.actor_user_id == query.actor_user_id)
    if query.before is not None:
        ts, row_id = query.before
        stmt = stmt.where(
            or_(
                AuditEvent.created_at < ts,
                and_(AuditEvent.created_at == ts, AuditEvent.id < row_id),
            )
        )
    return stmt


async def list_events(
    session: AsyncSession, query: AuditQuery
) -> tuple[list[tuple[AuditEvent, User | None]], bool]:
    """One page of events, newest first, and whether more follow. RLS keeps it to the org."""
    rows = (
        (
            await session.execute(
                _filtered(query)
                .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
                .limit(query.limit + 1)
            )
        )
        .tuples()
        .all()
    )
    return [(event, user) for event, user in rows[: query.limit]], len(rows) > query.limit
