"""Findings API: list with filters, read one, change its status (spec 003); triage across
series with groups, bulk changes, assignment, feedback, CSV export and saved filters (spec 029).

Lifecycle and deduplication rules are in ADR-0013. Deleting is not offered: a wrong finding
is resolved with a reason.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.auth.deps import client_ip
from tabayyun.authz import Principal, ReadScope, WriteScope, get_principal, get_session
from tabayyun.db.models import FINDING_FEEDBACK, FINDING_STATUSES, Finding, SavedFindingFilter
from tabayyun.services import findings as findings_service
from tabayyun.services import triage
from tabayyun.services.admin import audit
from tabayyun.services.findings import DIMENSIONS, MAX_REASON_CHARS, SEVERITIES, FindingFilter
from tabayyun.services.timeconv import datetime_to_ns, parse_time

router = APIRouter(prefix="/api/findings", tags=["findings"])
filters_router = APIRouter(prefix="/api/finding-filters", tags=["findings"])

StatusName = Literal["open", "acked", "muted", "resolved"]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
ActorDep = Annotated[Principal, Depends(get_principal)]


class AssigneeOut(BaseModel):
    """A user a finding is, or can be, assigned to."""

    id: str
    display_name: str


class FindingOut(BaseModel):
    """The `Finding` response of spec 003 with the triage fields of spec 029; `window` bounds
    are ns since the epoch."""

    id: str
    check_id: str
    series_id: str
    series_name: str
    asset_path: str | None
    dimension: str
    severity: str
    window: dict[str, int]
    score_impact: float
    summary: str
    evidence: dict[str, Any]
    status: str
    status_reason: str | None
    status_at: datetime | None
    muted_until: datetime | None
    assignee: AssigneeOut | None
    feedback: str | None
    first_run_id: str | None
    last_run_id: str | None
    occurrences: int
    created_at: datetime
    updated_at: datetime


class FindingList(BaseModel):
    """One page of findings and the cursor of the next page, if any."""

    items: list[FindingOut]
    next_cursor: str | None


class ChangeBody(BaseModel):
    """Body of `PATCH /api/findings/{id}` and the `change` of a bulk request: any of a status
    change (muting needs a reason and may name its end), an assignee and feedback."""

    model_config = ConfigDict(extra="forbid")

    status: StatusName | None = None
    reason: str | None = Field(default=None, max_length=MAX_REASON_CHARS)
    muted_until: datetime | None = None
    assignee_id: uuid.UUID | None = None
    feedback: str | None = None

    def to_change(self) -> triage.Change:
        """The fields the request sent; null is a value for `assignee_id` and `feedback`."""
        sent = self.model_fields_set
        until = self.muted_until
        if until is not None and until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        return triage.Change(
            status=self.status,
            reason=self.reason,
            muted_until=until,
            assignee_id=self.assignee_id if "assignee_id" in sent else triage.UNSET,
            feedback=self.feedback if "feedback" in sent else triage.UNSET,
        )


class BulkBody(BaseModel):
    """Body of `POST /api/findings/bulk`."""

    model_config = ConfigDict(extra="forbid")

    ids: list[uuid.UUID] = Field(min_length=1, max_length=triage.MAX_BULK)
    change: ChangeBody


class BulkFailureOut(BaseModel):
    """A finding a bulk change left alone, and why."""

    id: str
    error: Literal["not_found", "transition"]
    status: str | None


class BulkOut(BaseModel):
    """What a bulk change did."""

    updated: list[str]
    failed: list[BulkFailureOut]


class GroupOut(BaseModel):
    """One group of findings; `key` lists it as `check_id`, `series_id` or `asset_path`."""

    key: str
    label: str
    count: int
    severities: dict[str, int]
    latest: int = Field(description="Start of the newest window in the group, ns since the epoch")


class GroupList(BaseModel):
    """The groups, largest first; `truncated` when there were more than the limit."""

    items: list[GroupOut]
    truncated: bool


class AssigneeList(BaseModel):
    """The users findings of this workspace can be assigned to."""

    items: list[AssigneeOut]


class SavedFilterIn(BaseModel):
    """Body of `POST /api/finding-filters`."""

    model_config = ConfigDict(extra="forbid")

    name: str
    query: str


class SavedFilterOut(BaseModel):
    """A saved filter: its name and the findings page's URL search string."""

    id: str
    name: str
    query: str
    created_at: datetime


class SavedFilterList(BaseModel):
    """The caller's saved filters in the workspace, by name."""

    items: list[SavedFilterOut]


def _uuid_or_422(value: str | None, name: str) -> uuid.UUID | None:
    """Parse an optional UUID query parameter; 422 when malformed."""
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{name} is not a UUID") from exc


def _choices(value: str | None, allowed: tuple[str, ...], name: str) -> tuple[str, ...] | None:
    """Comma list restricted to `allowed`; None when absent."""
    if value is None:
        return None
    items = tuple(dict.fromkeys(v.strip() for v in value.split(",") if v.strip()))
    unknown = [v for v in items if v not in allowed]
    if not items or unknown:
        raise HTTPException(
            status_code=422, detail=f"{name} must be a comma list of {', '.join(allowed)}; got {value!r}"
        )
    return items


def _time_or_422(value: str | None, name: str) -> datetime | None:
    """Parse an optional RFC 3339 or epoch-ns query parameter; 422 when malformed."""
    if value is None:
        return None
    try:
        return parse_time(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{name}: {exc}") from exc


def _statuses(value: str | None) -> tuple[str, ...] | None:
    """Default open and acked; `all` means every status."""
    if value is None:
        return findings_service.DEFAULT_LIST_STATUSES
    if value.strip() == "all":
        return None
    return _choices(value, FINDING_STATUSES, "status")


def _assignee(value: str | None, principal: Principal) -> tuple[uuid.UUID | None, bool]:
    """`me`, `none` or a user id as `(assignee_id, unassigned)`."""
    if value is None:
        return None, False
    if value == "none":
        return None, True
    if value == "me":
        return principal.user_id, False
    return _uuid_or_422(value, "assignee"), False


def _feedback(value: str | None) -> str | None:
    if value is not None and value != "none" and value not in FINDING_FEEDBACK:
        raise HTTPException(
            status_code=422, detail=f"feedback must be one of {', '.join(FINDING_FEEDBACK)} or none"
        )
    return value


async def finding_filter(
    principal: ActorDep,
    series_id: str | None = None,
    check_id: Annotated[str | None, Query(max_length=128)] = None,
    severity: str | None = None,
    dimension: str | None = None,
    status: str | None = None,
    run_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    assignee: str | None = None,
    feedback: str | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    asset: Annotated[str | None, Query(max_length=512)] = None,
    asset_path: Annotated[str | None, Query(max_length=512)] = None,
    source_id: str | None = None,
) -> FindingFilter:
    """The filters of the findings list, shared by the list, its groups and the export."""
    if dimension is not None and dimension not in DIMENSIONS:
        raise HTTPException(status_code=422, detail=f"dimension must be one of {', '.join(DIMENSIONS)}")
    assignee_id, unassigned = _assignee(assignee, principal)
    return FindingFilter(
        series_id=_uuid_or_422(series_id, "series_id"),
        check_id=check_id,
        severities=_choices(severity, SEVERITIES, "severity"),
        dimension=dimension,
        statuses=_statuses(status),
        run_id=_uuid_or_422(run_id, "run_id"),
        since=_time_or_422(since, "since"),
        until=_time_or_422(until, "until"),
        assignee_id=assignee_id,
        unassigned=unassigned,
        feedback=_feedback(feedback),
        text=(q or "").strip() or None,
        asset=asset.strip() if asset and asset.strip() else None,
        asset_path=asset_path,
        source_id=_uuid_or_422(source_id, "source_id"),
    )


FilterDep = Annotated[FindingFilter, Depends(finding_filter)]


async def _out(session: AsyncSession, findings: list[Finding]) -> list[FindingOut]:
    context = await findings_service.context_of(session, findings)
    return [FindingOut(**findings_service.finding_to_dict(f, context)) for f in findings]


def _actor(request: Request, principal: Principal) -> audit.Actor:
    return audit.Actor(
        user_id=principal.user_id,
        org_id=principal.org_id,
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )


def _unprocessable(exc: triage.ChangeError) -> HTTPException:
    return HTTPException(
        status_code=422, detail=[{"loc": ["body", exc.field], "msg": exc.message, "type": "value_error"}]
    )


@router.get("", response_model=FindingList)
async def list_findings(
    session: SessionDep,
    scope: ReadScope,
    filters: FilterDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    cursor: str | None = None,
) -> FindingList:
    """Findings whose window overlaps `[since, until)`, newest window first."""
    try:
        rows, next_cursor = await findings_service.list_findings(
            session, scope, filters=filters, limit=limit, cursor=cursor
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return FindingList(items=await _out(session, rows), next_cursor=next_cursor)


@router.get("/groups", response_model=GroupList)
async def list_groups(
    session: SessionDep, scope: ReadScope, filters: FilterDep, by: triage.GroupBy
) -> GroupList:
    """The filtered findings grouped by check, series or asset path, largest group first."""
    items, truncated = await triage.groups(session, scope, filters, by)
    return GroupList(
        items=[GroupOut(**{**g, "latest": datetime_to_ns(g["latest"])}) for g in items], truncated=truncated
    )


@router.get("/assignees", response_model=AssigneeList)
async def list_assignees(session: SessionDep, scope: ReadScope) -> AssigneeList:
    """The users with at least the editor role in the workspace, by name."""
    users = await triage.assignable_users(session, scope)
    return AssigneeList(items=[AssigneeOut(id=str(uid), display_name=name) for uid, name in users])


@router.get("/export")
async def export_findings(session: SessionDep, scope: ReadScope, filters: FilterDep) -> Response:
    """The filtered findings as CSV, newest window first, at most 50 000 rows;
    `X-Tabayyun-Truncated: true` when the list was cut."""
    body, truncated = await triage.export_csv(session, scope, filters)
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="findings.csv"',
            "X-Tabayyun-Truncated": "true" if truncated else "false",
        },
    )


@router.post("/bulk", response_model=BulkOut)
async def change_many(
    request: Request, body: BulkBody, session: SessionDep, scope: WriteScope, principal: ActorDep
) -> BulkOut:
    """Apply one change to many findings. Each is changed on its own terms: one whose lifecycle
    forbids the move, or that does not exist here, is reported and the others are changed."""
    now = datetime.now(UTC)
    change = body.change.to_change()
    try:
        await triage.validate_change(session, scope, change, now)
    except triage.ChangeError as exc:
        raise _unprocessable(exc) from exc
    updated, failed = await triage.apply_bulk(
        session, scope, body.ids, change, actor=_actor(request, principal), now=now
    )
    return BulkOut(
        updated=[str(i) for i in updated],
        failed=[BulkFailureOut(id=str(f.id), error=f.error, status=f.status) for f in failed],
    )


def _finding_id(finding_id: str) -> uuid.UUID:
    """Parse a finding id from the path; a malformed id is simply not found (404)."""
    try:
        return uuid.UUID(finding_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="finding not found") from exc


@router.get("/{finding_id}", response_model=FindingOut)
async def get_finding(finding_id: str, session: SessionDep, scope: ReadScope) -> FindingOut:
    """One finding; 404 for unknown ids."""
    finding = await findings_service.get_finding(session, scope, _finding_id(finding_id))
    if finding is None:
        raise HTTPException(status_code=404, detail="finding not found")
    return (await _out(session, [finding]))[0]


@router.patch("/{finding_id}", response_model=FindingOut)
async def change_finding(
    finding_id: str,
    request: Request,
    body: Annotated[ChangeBody, Body()],
    session: SessionDep,
    scope: WriteScope,
    principal: ActorDep,
) -> FindingOut:
    """Move a finding through its lifecycle, assign it or mark it; 409 with the current status
    on a forbidden move."""
    now = datetime.now(UTC)
    change = body.to_change()
    try:
        await triage.validate_change(session, scope, change, now)
        finding = await triage.apply_change(
            session, scope, _finding_id(finding_id), change, actor=_actor(request, principal), now=now
        )
    except triage.ChangeError as exc:
        raise _unprocessable(exc) from exc
    except findings_service.FindingNotFoundError as exc:
        raise HTTPException(status_code=404, detail="finding not found") from exc
    except findings_service.TransitionError as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc), "status": exc.current}) from exc
    return (await _out(session, [finding]))[0]


# Saved filters (spec 029): personal, per workspace


def _filter_out(row: SavedFindingFilter) -> SavedFilterOut:
    return SavedFilterOut(id=str(row.id), name=row.name, query=row.query, created_at=row.created_at)


@filters_router.get("", response_model=SavedFilterList)
async def list_saved_filters(session: SessionDep, scope: ReadScope, principal: ActorDep) -> SavedFilterList:
    """The caller's saved filters in the workspace, by name."""
    rows = await triage.list_filters(session, scope, principal.user_id)
    return SavedFilterList(items=[_filter_out(r) for r in rows])


@filters_router.post("", response_model=SavedFilterOut, status_code=201)
async def save_filter(
    body: SavedFilterIn, session: SessionDep, scope: ReadScope, principal: ActorDep
) -> SavedFilterOut:
    """Save a filter for the caller; viewers may, it is theirs alone. 409 `name_taken` or
    `too_many`."""
    try:
        row = await triage.save_filter(session, scope, principal.user_id, name=body.name, query=body.query)
    except triage.ChangeError as exc:
        raise _unprocessable(exc) from exc
    except triage.FilterNameTakenError as exc:
        raise HTTPException(status_code=409, detail="name_taken") from exc
    except triage.TooManyFiltersError as exc:
        raise HTTPException(status_code=409, detail="too_many") from exc
    return _filter_out(row)


@filters_router.delete("/{filter_id}", status_code=204)
async def delete_saved_filter(
    filter_id: str, session: SessionDep, scope: ReadScope, principal: ActorDep
) -> Response:
    """Delete one of the caller's own filters; 404 for anyone else's."""
    try:
        parsed = uuid.UUID(filter_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="filter not found") from exc
    if not await triage.delete_filter(session, scope, principal.user_id, parsed):
        raise HTTPException(status_code=404, detail="filter not found")
    return Response(status_code=204)
