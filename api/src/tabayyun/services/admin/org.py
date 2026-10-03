"""The org itself (spec 014): read and rename."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.db.models import Org
from tabayyun.services.admin import audit
from tabayyun.services.admin.errors import NotFoundError
from tabayyun.services.admin.names import clean_name


async def get_org(session: AsyncSession, org_id: object) -> Org:
    """The caller's org (RLS shows no other)."""
    org = await session.scalar(select(Org).where(Org.id == org_id))
    if org is None:
        raise NotFoundError
    return org


async def rename_org(session: AsyncSession, actor: audit.Actor, name: str) -> Org:
    """Rename the org; the same name writes no event."""
    org = await get_org(session, actor.org_id)
    cleaned = clean_name(name)
    if cleaned != org.name:
        before, org.name = org.name, cleaned
        await audit.record(
            session, actor, "org.renamed", "org", org.id, details=audit.change(before, cleaned)
        )
        await session.flush()
    return org
