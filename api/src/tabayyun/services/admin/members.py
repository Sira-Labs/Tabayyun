"""Org members (spec 014): list, change role, remove.

The bootstrap user (spec 007) is a placeholder, not a person: it is never listed, never a target
and never counts as an owner.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.authz import OrgRole, Principal
from tabayyun.db.models import (
    BOOTSTRAP_USER_ID,
    AuthSession,
    OrgMembership,
    TeamMember,
    User,
    WorkspaceMembership,
)
from tabayyun.services.admin import audit
from tabayyun.services.admin.errors import InvalidError, NotFoundError
from tabayyun.services.admin.rules import check_role_change


@dataclass(frozen=True)
class Member:
    """A person in the org."""

    user_id: uuid.UUID
    email: str
    display_name: str
    role: OrgRole
    joined_at: datetime
    disabled: bool


def _member(m: OrgMembership, u: User) -> Member:
    return Member(
        user_id=u.id,
        email=u.email,
        display_name=u.display_name,
        role=OrgRole(m.role),
        joined_at=m.created_at,
        disabled=u.disabled_at is not None,
    )


async def org_role(session: AsyncSession, principal: Principal) -> OrgRole | None:
    """The principal's role in their org while their user is active, else None."""
    role = await session.scalar(
        select(OrgMembership.role)
        .join(User, User.id == OrgMembership.user_id)
        .where(
            OrgMembership.org_id == principal.org_id,
            OrgMembership.user_id == principal.user_id,
            User.disabled_at.is_(None),
        )
    )
    return None if role is None else OrgRole(role)


def encode_cursor(email: str) -> str:
    """Opaque cursor after `email` (members are sorted by their unique email)."""
    return base64.urlsafe_b64encode(email.encode()).decode()


def decode_cursor(cursor: str) -> str:
    """Inverse of `encode_cursor`; InvalidError("invalid_cursor") on anything else."""
    try:
        return base64.urlsafe_b64decode(cursor.encode()).decode()
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise InvalidError("invalid_cursor") from exc


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def list_members(
    session: AsyncSession, org_id: uuid.UUID, *, q: str | None, after: str | None, limit: int
) -> tuple[list[Member], str | None]:
    """One page of members by email; `q` matches email or name, case-insensitively."""
    stmt = (
        select(OrgMembership, User)
        .join(User, User.id == OrgMembership.user_id)
        .where(OrgMembership.org_id == org_id, OrgMembership.user_id != BOOTSTRAP_USER_ID)
    )
    if q and q.strip():
        pattern = f"%{_escape_like(q.strip())}%"
        stmt = stmt.where(
            or_(User.email.ilike(pattern, escape="\\"), User.display_name.ilike(pattern, escape="\\"))
        )
    if after is not None:
        stmt = stmt.where(User.email > after)
    rows = (await session.execute(stmt.order_by(User.email).limit(limit + 1))).tuples().all()
    page = [_member(m, u) for m, u in rows[:limit]]
    return page, encode_cursor(page[-1].email) if len(rows) > limit else None


async def get_member(session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> Member:
    """One member; NotFoundError for the bootstrap user, an unknown user or another org's."""
    if user_id == BOOTSTRAP_USER_ID:
        raise NotFoundError
    row = (
        (
            await session.execute(
                select(OrgMembership, User)
                .join(User, User.id == OrgMembership.user_id)
                .where(OrgMembership.org_id == org_id, OrgMembership.user_id == user_id)
            )
        )
        .tuples()
        .one_or_none()
    )
    if row is None:
        raise NotFoundError
    return _member(*row)


async def active_owners(session: AsyncSession, org_id: uuid.UUID) -> int:
    """Owners whose user is not disabled, the bootstrap user aside."""
    count = await session.scalar(
        select(func.count())
        .select_from(OrgMembership)
        .join(User, User.id == OrgMembership.user_id)
        .where(
            OrgMembership.org_id == org_id,
            OrgMembership.role == OrgRole.OWNER.value,
            OrgMembership.user_id != BOOTSTRAP_USER_ID,
            User.disabled_at.is_(None),
        )
    )
    return int(count or 0)


async def change_role(
    session: AsyncSession, actor: audit.Actor, actor_role: OrgRole, user_id: uuid.UUID, new: OrgRole
) -> Member:
    """Set a member's org role under the owner rules; a no-op writes no event."""
    member = await get_member(session, actor.org_id, user_id)
    if member.role is new:
        return member
    check_role_change(actor_role, member.role, new, await active_owners(session, actor.org_id))
    await session.execute(
        update(OrgMembership)
        .where(OrgMembership.org_id == actor.org_id, OrgMembership.user_id == user_id)
        .values(role=new.value)
    )
    await audit.record(
        session,
        actor,
        "member.role_changed",
        "user",
        user_id,
        details={"email": member.email, **audit.change(member.role.value, new.value)},
    )
    return Member(**{**member.__dict__, "role": new})


async def remove_member(
    session: AsyncSession, actor: audit.Actor, actor_role: OrgRole, user_id: uuid.UUID
) -> None:
    """Remove a member with their workspace grants and team memberships in the org, and revoke
    their sessions in it, so their next request gets 401."""
    member = await get_member(session, actor.org_id, user_id)
    check_role_change(actor_role, member.role, None, await active_owners(session, actor.org_id))
    for table in (WorkspaceMembership, TeamMember):
        await session.execute(delete(table).where(table.org_id == actor.org_id, table.user_id == user_id))
    await session.execute(
        delete(OrgMembership).where(OrgMembership.org_id == actor.org_id, OrgMembership.user_id == user_id)
    )
    await session.execute(
        update(AuthSession)
        .where(
            AuthSession.user_id == user_id,
            AuthSession.org_id == actor.org_id,
            AuthSession.revoked_at.is_(None),
        )
        .values(revoked_at=func.now())
    )
    await audit.record(
        session,
        actor,
        "member.removed",
        "user",
        user_id,
        details={"email": member.email, "role": member.role.value},
    )
