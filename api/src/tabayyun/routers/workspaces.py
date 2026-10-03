"""Workspaces the signed-in user may open (spec 014): what the web app's picker lists.

The chosen workspace travels as `X-Tabayyun-Workspace` on every data request
(`tabayyun.authz.deps.get_workspace_id`).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.authz import Principal, WorkspaceRole, get_principal, get_session, workspace_roles

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


class WorkspaceOut(BaseModel):
    """A workspace and the caller's effective role in it."""

    id: str
    name: str
    timezone: str
    role: WorkspaceRole


@router.get("", response_model=list[WorkspaceOut])
async def list_workspaces(
    session: Annotated[AsyncSession, Depends(get_session)],
    principal: Annotated[Principal, Depends(get_principal)],
) -> list[WorkspaceOut]:
    """The workspaces in which the caller holds any role, by name."""
    return [
        WorkspaceOut(id=str(w.id), name=w.name, timezone=w.timezone, role=role)
        for w, role in await workspace_roles(session, principal)
    ]
