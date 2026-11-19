"""Live events (spec 030): what happened in a workspace, told to the browsers that watch it.

An event is published with `publish` inside the transaction that made it true; Postgres
delivers it on commit to every API process (`tabayyun.events.listener`), whose `Broker` hands
it to the open streams of that workspace (`tabayyun.events.stream`).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.authz.scope import Scope

# The Postgres notification channel every event travels on.
CHANNEL = "tabayyun_events"
# Version of the payload; a process drops versions it does not know.
VERSION = 1

RUN_COMPLETED = "run.completed"
FINDING_CREATED = "finding.created"
# Not published by anyone: told to a stream that may have missed events.
RESYNC = "resync"

_NOTIFY = text("SELECT pg_notify(:channel, :payload)")


class MalformedEventError(ValueError):
    """A notification on the channel that is not an event this version understands."""


@dataclass(frozen=True)
class Event:
    """Something that happened in one workspace. `data` holds ids, a status and counts only:
    the channel is not protected by row-level security."""

    type: str
    org_id: uuid.UUID
    workspace_id: uuid.UUID
    data: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def encode(self) -> str:
        """The notification payload."""
        return json.dumps(
            {
                "v": VERSION,
                "type": self.type,
                "org_id": str(self.org_id),
                "workspace_id": str(self.workspace_id),
                "at": self.at.isoformat(),
                "data": self.data,
            },
            separators=(",", ":"),
        )

    @classmethod
    def decode(cls, payload: str) -> Event:
        """The event of a notification payload; `MalformedEventError` for anything else."""
        try:
            raw = json.loads(payload)
            if not isinstance(raw, dict) or raw.get("v") != VERSION:
                raise MalformedEventError("unknown version")
            data = raw.get("data", {})
            if not isinstance(raw["type"], str) or not isinstance(data, dict):
                raise MalformedEventError("wrong types")
            return cls(
                type=raw["type"],
                org_id=uuid.UUID(raw["org_id"]),
                workspace_id=uuid.UUID(raw["workspace_id"]),
                data=data,
                at=datetime.fromisoformat(raw["at"]),
            )
        except MalformedEventError:
            raise
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            # ValueError covers bad JSON, a bad UUID and a bad timestamp.
            raise MalformedEventError(str(exc)) from exc

    def public(self) -> dict[str, Any]:
        """What a stream tells its client: the type, the time and the data, without the ids
        of the org and the workspace."""
        return {"type": self.type, "at": self.at.isoformat(), **self.data}


async def publish(session: AsyncSession, scope: Scope, type_: str, data: dict[str, Any]) -> None:
    """Publish an event in `session`'s transaction: delivered when it commits, dropped when
    it rolls back."""
    event = Event(type=type_, org_id=scope.org_id, workspace_id=scope.workspace_id, data=data)
    await session.execute(_NOTIFY, {"channel": CHANNEL, "payload": event.encode()})


async def publish_run_completed(
    session: AsyncSession, scope: Scope, run_id: uuid.UUID, status: str, *, new_findings: int = 0
) -> None:
    """A run turned `succeeded` or `failed`; with new findings, `finding.created` follows."""
    await publish(
        session,
        scope,
        RUN_COMPLETED,
        {"run_id": str(run_id), "status": status, "n_findings_new": new_findings},
    )
    if new_findings > 0:
        await publish(session, scope, FINDING_CREATED, {"run_id": str(run_id), "count": new_findings})
