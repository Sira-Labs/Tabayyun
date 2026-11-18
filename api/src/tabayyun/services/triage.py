"""Triage of findings (spec 029): changes with their audit trail, bulk changes, groups, the
CSV export, assignable users, saved filters and the end of mutes.

The lifecycle itself is ADR-0013's and lives in `services.findings`; this module adds who
works on a finding, the user's verdict on it, and the record of every change.
"""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tabayyun.authz.roles import Action, OrgRole, WorkspaceRole, allows, effective_role
from tabayyun.authz.scope import Scope
from tabayyun.db.models import (
    FINDING_FEEDBACK,
    Finding,
    OrgMembership,
    SavedFindingFilter,
    Series,
    TeamMember,
    User,
    WorkspaceMembership,
    WorkspaceTeamRole,
)
from tabayyun.services import findings as findings_service
from tabayyun.services.admin import audit
from tabayyun.services.findings import SEVERITIES, FindingFilter, FindingNotFoundError, TransitionError

log = structlog.get_logger()

MAX_MUTE = timedelta(days=366)
MAX_BULK = 500
MAX_GROUPS = 500
MAX_EXPORT_ROWS = 50_000
EXPORT_PAGE = 1_000
MAX_FILTERS = 50
MAX_FILTER_NAME = 80
MAX_FILTER_QUERY = 2_000
REOPEN_SQL = text("SELECT tabayyun_reopen_expired_mutes(:now)")
EXPORT_COLUMNS = (
    "id",
    "series",
    "asset_path",
    "check_id",
    "dimension",
    "severity",
    "status",
    "window_start",
    "window_end",
    "summary",
    "assignee",
    "feedback",
    "status_reason",
    "muted_until",
    "occurrences",
    "created_at",
)

GroupBy = Literal["check", "series", "asset"]


class _Unset:
    """Marks a field the request did not send (null is a value for assignee and feedback)."""


UNSET = _Unset()


@dataclass(frozen=True)
class Change:
    """What one request changes on a finding; `UNSET` fields stay as they are."""

    status: str | None = None
    reason: str | None = None
    muted_until: datetime | None = None
    assignee_id: uuid.UUID | None | _Unset = UNSET
    feedback: str | None | _Unset = UNSET

    @property
    def empty(self) -> bool:
        """Whether the request names nothing to change."""
        return self.status is None and self.assignee_id is UNSET and self.feedback is UNSET


class ChangeError(ValueError):
    """The change itself is not valid (422): the field and what is wrong with it."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


class FilterNameTakenError(Exception):
    """The user already has a saved filter of that name in the workspace (409)."""


class TooManyFiltersError(Exception):
    """The user has the maximum number of saved filters in the workspace (409)."""


# Who can be assigned


async def assignable_users(session: AsyncSession, scope: Scope) -> list[tuple[uuid.UUID, str]]:
    """Users with at least the editor role in the workspace, as `(id, display_name)` by name.

    Roles add up as in `authz.policy`: org owners and admins, direct members, team members.
    """
    members = await session.execute(
        select(User.id, User.display_name, OrgMembership.role)
        .join(OrgMembership, OrgMembership.user_id == User.id)
        .where(OrgMembership.org_id == scope.org_id, User.disabled_at.is_(None))
    )
    direct = dict(
        (
            await session.execute(
                select(WorkspaceMembership.user_id, WorkspaceMembership.role).where(
                    WorkspaceMembership.workspace_id == scope.workspace_id
                )
            )
        ).all()
    )
    via_team: dict[uuid.UUID, list[WorkspaceRole]] = {}
    team_rows = await session.execute(
        select(TeamMember.user_id, WorkspaceTeamRole.role)
        .join(WorkspaceTeamRole, WorkspaceTeamRole.team_id == TeamMember.team_id)
        .where(WorkspaceTeamRole.workspace_id == scope.workspace_id)
    )
    for member_id, team_role in team_rows:
        via_team.setdefault(member_id, []).append(WorkspaceRole(team_role))
    out = []
    for user_id, name, org_role in members:
        own = direct.get(user_id)
        effective = effective_role(
            OrgRole(org_role), None if own is None else WorkspaceRole(own), via_team.get(user_id, [])
        )
        if allows(effective, Action.WRITE):
            out.append((user_id, name))
    return sorted(out, key=lambda user: (user[1].casefold(), str(user[0])))


# Changes


async def validate_change(session: AsyncSession, scope: Scope, change: Change, now: datetime) -> None:
    """Check the change itself, whatever finding it is applied to.

    Raises:
        ChangeError: nothing to change, a mute end that is misplaced, past or too far ahead,
            an assignee without the editor role in the workspace, or unknown feedback.
    """
    if change.empty:
        raise ChangeError("change", "name at least one of status, assignee_id and feedback")
    if change.status == "muted" and not (change.reason or "").strip():
        raise ChangeError("reason", "muting a finding needs a non-empty reason")
    if change.muted_until is not None:
        if change.status != "muted":
            raise ChangeError("muted_until", "only with status muted")
        if change.muted_until <= now:
            raise ChangeError("muted_until", "must lie in the future")
        if change.muted_until > now + MAX_MUTE:
            raise ChangeError("muted_until", f"at most {MAX_MUTE.days} days ahead")
    if isinstance(change.assignee_id, uuid.UUID):
        allowed = {user_id for user_id, _ in await assignable_users(session, scope)}
        if change.assignee_id not in allowed:
            raise ChangeError("assignee_id", "not a user with the editor role in this workspace")
    if isinstance(change.feedback, str) and change.feedback not in FINDING_FEEDBACK:
        raise ChangeError("feedback", f"must be one of {', '.join(FINDING_FEEDBACK)} or null")


def _text(value: uuid.UUID | None) -> str | None:
    return None if value is None else str(value)


async def apply_change(
    session: AsyncSession,
    scope: Scope,
    finding_id: uuid.UUID,
    change: Change,
    *,
    actor: audit.Actor,
    now: datetime,
) -> Finding:
    """Apply a validated change to one finding and record what it changed, in the caller's
    transaction. A part that changes nothing writes no event.

    Raises:
        FindingNotFoundError: no such finding in the workspace.
        TransitionError: the lifecycle forbids the status change; nothing was changed.
    """
    finding = await findings_service.get_finding(session, scope, finding_id, for_update=True)
    if finding is None:
        raise FindingNotFoundError(str(finding_id))
    if change.status is not None and change.status not in findings_service.TRANSITIONS[finding.status]:
        raise TransitionError(finding.status, change.status)

    async def record(action: str, details: dict[str, Any]) -> None:
        await audit.record(
            session, actor, action, "finding", finding_id, workspace_id=scope.workspace_id, details=details
        )

    if change.status is not None:
        before = finding.status
        finding.status = change.status
        finding.status_reason = change.reason.strip() if change.reason and change.reason.strip() else None
        finding.status_at = now
        # Any move ends a mute; only a new mute sets its end.
        finding.muted_until = change.muted_until if change.status == "muted" else None
        details = {"from": before, "to": change.status, "reason": finding.status_reason}
        if finding.muted_until is not None:
            details["muted_until"] = finding.muted_until.isoformat()
        await record("finding.status_changed", details)
    if not isinstance(change.assignee_id, _Unset) and change.assignee_id != finding.assignee_id:
        details = {"from": _text(finding.assignee_id), "to": _text(change.assignee_id)}
        finding.assignee_id = change.assignee_id
        await record("finding.assigned", details)
    if not isinstance(change.feedback, _Unset) and change.feedback != finding.feedback:
        details = {"from": finding.feedback, "to": change.feedback}
        finding.feedback = change.feedback
        finding.feedback_at = None if change.feedback is None else now
        await record("finding.feedback", details)
    finding.updated_at = now
    await session.flush()
    log.info("finding.changed", finding_id=str(finding_id), status=finding.status)
    return finding


@dataclass(frozen=True)
class BulkFailure:
    """A finding a bulk change left alone: `not_found`, or `transition` with its status."""

    id: uuid.UUID
    error: Literal["not_found", "transition"]
    status: str | None = None


async def apply_bulk(
    session: AsyncSession,
    scope: Scope,
    finding_ids: Sequence[uuid.UUID],
    change: Change,
    *,
    actor: audit.Actor,
    now: datetime,
) -> tuple[list[uuid.UUID], list[BulkFailure]]:
    """Apply a validated change to each finding on its own terms, in id order (so two bulk
    requests lock rows in the same order). Returns the changed ids and the failures."""
    updated: list[uuid.UUID] = []
    failed: list[BulkFailure] = []
    for finding_id in sorted(set(finding_ids), key=str):
        try:
            await apply_change(session, scope, finding_id, change, actor=actor, now=now)
        except FindingNotFoundError:
            failed.append(BulkFailure(finding_id, "not_found"))
        except TransitionError as exc:
            failed.append(BulkFailure(finding_id, "transition", exc.current))
        else:
            updated.append(finding_id)
    return updated, failed


async def reopen_expired_mutes(factory: async_sessionmaker[AsyncSession], now: datetime) -> int:
    """Open every muted finding whose end has passed, in every org. Returns how many."""
    async with factory() as session, session.begin():
        return int((await session.execute(REOPEN_SQL, {"now": now})).scalar_one())


# Groups


async def groups(
    session: AsyncSession, scope: Scope, filters: FindingFilter, by: GroupBy
) -> tuple[list[dict[str, Any]], bool]:
    """The filtered findings grouped by check, series or asset path, largest group first.

    Returns the groups (`key`, `label`, `count`, `severities`, `latest` as a datetime) and
    whether there were more than `MAX_GROUPS`.
    """
    asset = func.coalesce(Series.asset_path, "")
    columns: dict[str, tuple[Any, Any]] = {
        "check": (Finding.check_id, Finding.check_id),
        "series": (Finding.series_id, Series.name),
        "asset": (asset, asset),
    }
    key, label = columns[by]
    count = func.count().label("n")
    per_severity = [func.count().filter(Finding.severity == s).label(s) for s in SEVERITIES]
    stmt = (
        select(key.label("key"), label.label("label"), count, func.max(Finding.window_start), *per_severity)
        .select_from(Finding)
        .join(Series, Series.id == Finding.series_id)
        .where(*findings_service.filter_conditions(scope, filters))
        .group_by(key, label)
        .order_by(count.desc(), key)
        .limit(MAX_GROUPS + 1)
    )
    rows = (await session.execute(stmt)).all()
    items = [
        {
            "key": str(row[0]),
            "label": str(row[1]) if str(row[1]) else "No asset",
            "count": int(row[2]),
            "latest": row[3],
            "severities": {s: int(n) for s, n in zip(SEVERITIES, row[4:], strict=True) if n},
        }
        for row in rows[:MAX_GROUPS]
    ]
    return items, len(rows) > MAX_GROUPS


# Export


def _cell(value: Any) -> str:
    """A CSV cell that a spreadsheet does not run as a formula."""
    cell = "" if value is None else str(value)
    return f"'{cell}" if cell[:1] in ("=", "+", "-", "@") else cell


async def export_csv(
    session: AsyncSession, scope: Scope, filters: FindingFilter, *, max_rows: int = MAX_EXPORT_ROWS
) -> tuple[str, bool]:
    """The filtered findings as CSV text, newest window first, and whether `max_rows` cut it."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(EXPORT_COLUMNS)
    cursor: str | None = None
    written = 0
    while True:
        want = min(EXPORT_PAGE, max_rows - written)
        rows, cursor = await findings_service.list_findings(
            session, scope, filters=filters, limit=want, cursor=cursor
        )
        context = await findings_service.context_of(session, rows)
        for finding in rows:
            name, asset = context.series.get(finding.series_id, ("", None))
            assignee = None if finding.assignee_id is None else context.users.get(finding.assignee_id)
            values = (
                finding.id,
                name,
                asset,
                finding.check_id,
                finding.dimension,
                finding.severity,
                finding.status,
                finding.window_start.isoformat(),
                finding.window_end.isoformat(),
                finding.summary,
                assignee,
                finding.feedback,
                finding.status_reason,
                None if finding.muted_until is None else finding.muted_until.isoformat(),
                finding.occurrences,
                finding.created_at.isoformat(),
            )
            writer.writerow([_cell(v) for v in values])
        written += len(rows)
        if cursor is None:
            return out.getvalue(), False
        if written >= max_rows:
            return out.getvalue(), True


# Saved filters


def _clean_filter(name: str, query: str) -> tuple[str, str]:
    name = name.strip()
    if not 1 <= len(name) <= MAX_FILTER_NAME:
        raise ChangeError("name", f"must be 1 to {MAX_FILTER_NAME} characters")
    query = query.strip().lstrip("?")
    if len(query) > MAX_FILTER_QUERY:
        raise ChangeError("query", f"at most {MAX_FILTER_QUERY} characters")
    return name, query


async def list_filters(session: AsyncSession, scope: Scope, user_id: uuid.UUID) -> list[SavedFindingFilter]:
    """The user's saved filters in the workspace, by name."""
    stmt = (
        select(SavedFindingFilter)
        .where(SavedFindingFilter.workspace_id == scope.workspace_id, SavedFindingFilter.user_id == user_id)
        .order_by(func.lower(SavedFindingFilter.name), SavedFindingFilter.id)
    )
    return list((await session.execute(stmt)).scalars())


async def save_filter(
    session: AsyncSession, scope: Scope, user_id: uuid.UUID, *, name: str, query: str
) -> SavedFindingFilter:
    """Save a filter for the user.

    Raises:
        ChangeError: the name or the query is too long or empty.
        FilterNameTakenError, TooManyFiltersError: 409s of the route.
    """
    name, query = _clean_filter(name, query)
    existing = await list_filters(session, scope, user_id)
    if any(f.name == name for f in existing):
        raise FilterNameTakenError
    if len(existing) >= MAX_FILTERS:
        raise TooManyFiltersError
    row = SavedFindingFilter(
        org_id=scope.org_id, workspace_id=scope.workspace_id, user_id=user_id, name=name, query=query
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:  # a concurrent save of the same name
        raise FilterNameTakenError from exc
    await session.refresh(row)
    return row


async def delete_filter(
    session: AsyncSession, scope: Scope, user_id: uuid.UUID, filter_id: uuid.UUID
) -> bool:
    """Delete the user's own filter; False when there is none with that id."""
    row = (
        await session.execute(
            select(SavedFindingFilter).where(
                SavedFindingFilter.id == filter_id,
                SavedFindingFilter.workspace_id == scope.workspace_id,
                SavedFindingFilter.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True
