"""Findings, metrics and scores: persistence with deduplication, queries, status changes.

Spec 003, lifecycle in ADR-0013. `persist_report` runs inside the run's completion
transaction (spec 002), so a run that fails persists nothing.

Deduplication: an incoming finding merges into an existing finding of the same series and
check that is `open` or `acked`, has the same evidence shape (its set of top-level evidence
keys, which tells a gap from the whole-window completeness finding of the same check), was
not created by the same run, and overlaps the incoming window. Among several candidates the
one with the largest overlap ratio (intersection over union) wins. `muted` and `resolved`
findings never absorb: a problem re-reported after resolution is a new finding.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import or_, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun import core
from tabayyun.db.models import (
    DEFAULT_ORG_ID,
    DEFAULT_WORKSPACE_ID,
    FINDING_STATUSES,
    Finding,
    Metric,
    Score,
)
from tabayyun.services.pagination import decode_keyset, encode_keyset
from tabayyun.services.timeconv import datetime_to_ns, ns_to_datetime, ns_to_datetime_ceil

log = structlog.get_logger()

SEVERITIES = ("low", "medium", "high", "critical")
DIMENSIONS = (
    "completeness",
    "timeliness",
    "validity",
    "accuracy",
    "consistency",
    "plausibility",
    "integrity",
)
ABSORBING_STATUSES = ("open", "acked")
DEFAULT_LIST_STATUSES = ("open", "acked")
MAX_REASON_CHARS = 500
# Allowed status changes (spec 003 behaviour 3); anything else is a conflict.
TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"acked", "muted", "resolved"}),
    "acked": frozenset({"resolved", "open"}),
    "muted": frozenset({"open"}),
    "resolved": frozenset({"open"}),
}
# First key of `pg_advisory_xact_lock(int, int)`; the second is a hash of the series id, so
# two runs of the same series deduplicate one after the other instead of racing.
FINDINGS_LOCK_NAMESPACE = 7_412_003
ONE_MICROSECOND = timedelta(microseconds=1)


@dataclass(frozen=True)
class PersistOutcome:
    """How the findings of one report landed: newly inserted or merged into existing ones."""

    new: int
    merged: int
    metrics: int


class FindingNotFoundError(Exception):
    """No finding with that id in the caller's workspace."""


class TransitionError(Exception):
    """A status change the lifecycle does not allow; carries the current status."""

    def __init__(self, current: str, requested: str) -> None:
        """Remember the current and the requested status for the 409 response."""
        super().__init__(f"cannot change status from {current} to {requested}")
        self.current = current
        self.requested = requested


@dataclass(frozen=True)
class FindingFilter:
    """Filters of `GET /api/findings`; `statuses=None` means every status."""

    series_id: uuid.UUID | None = None
    check_id: str | None = None
    severities: tuple[str, ...] | None = None
    dimension: str | None = None
    statuses: tuple[str, ...] | None = DEFAULT_LIST_STATUSES
    run_id: uuid.UUID | None = None
    since: datetime | None = None
    until: datetime | None = None


# Persistence (worker path, inside the run's transaction)


def _window(finding: core.Finding) -> tuple[datetime, datetime]:
    """Stored bounds of a core window: start rounded down, end rounded up, never empty."""
    start = ns_to_datetime(finding.window["start"])
    end = ns_to_datetime_ceil(finding.window["end"])
    return start, max(end, start + ONE_MICROSECOND)


def _shape(evidence: dict[str, Any]) -> frozenset[str]:
    """Evidence shape: the set of top-level evidence keys, which names the kind of finding."""
    return frozenset(evidence)


def _overlap_ratio(a: tuple[datetime, datetime], b: tuple[datetime, datetime]) -> float:
    """Intersection over union of two half-open windows."""
    inter = (min(a[1], b[1]) - max(a[0], b[0])).total_seconds()
    union = (max(a[1], b[1]) - min(a[0], b[0])).total_seconds()
    return max(inter, 0.0) / union if union > 0 else 0.0


async def _best_candidate(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    series_id: uuid.UUID,
    incoming: core.Finding,
    window: tuple[datetime, datetime],
) -> Finding | None:
    """The existing finding the incoming one merges into, or None (rule in the module docstring)."""
    start, end = window
    stmt = (
        select(Finding)
        .where(
            Finding.workspace_id == DEFAULT_WORKSPACE_ID,
            Finding.series_id == series_id,
            Finding.check_id == incoming.check_id,
            Finding.status.in_(ABSORBING_STATUSES),
            Finding.window_start < end,
            Finding.window_end > start,
            or_(Finding.first_run_id.is_(None), Finding.first_run_id != run_id),
        )
        .order_by(Finding.window_start, Finding.created_at)
        .with_for_update()
    )
    shape = _shape(incoming.evidence)
    candidates = [f for f in (await session.execute(stmt)).scalars() if _shape(f.evidence) == shape]
    if not candidates:
        return None
    # max() keeps the first of equal ratios, i.e. the earliest window (query order).
    return max(candidates, key=lambda f: _overlap_ratio((f.window_start, f.window_end), window))


async def _merge(
    session: AsyncSession,
    existing: Finding,
    *,
    run_id: uuid.UUID,
    incoming: core.Finding,
    window: tuple[datetime, datetime],
    now: datetime,
) -> None:
    """Fold the incoming finding into `existing`: union window, newest facts, one occurrence per run."""
    new_start = min(existing.window_start, window[0])
    fields: dict[str, Any] = {
        "window_end": max(existing.window_end, window[1]),
        "severity": incoming.severity,
        "dimension": incoming.dimension,
        "score_impact": incoming.score_impact,
        "summary": incoming.summary,
        "evidence": incoming.evidence,
        # Occurrences count runs: a second incoming finding of the same run adds nothing.
        "occurrences": existing.occurrences + (0 if existing.last_run_id == run_id else 1),
        "last_run_id": run_id,
        "updated_at": now,
    }
    if new_start == existing.window_start:
        for key, value in fields.items():
            setattr(existing, key, value)
        await session.flush()
        return
    # `window_start` is part of the primary key and the hypertable's partition column, so a
    # window that grows backwards is rewritten as delete + insert under the same id.
    keep = {
        name: getattr(existing, name)
        for name in (
            "id",
            "org_id",
            "workspace_id",
            "series_id",
            "check_id",
            "status",
            "status_reason",
            "status_at",
            "first_run_id",
            "created_at",
        )
    }
    await session.delete(existing)
    await session.flush()
    session.add(Finding(**keep, window_start=new_start, **fields))
    await session.flush()


def _metric_rows(
    run_id: uuid.UUID, series_id: uuid.UUID, metrics: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """One row per (check, name, stored ts); null values (NaN in the core) are skipped.

    `metrics.ts` has microsecond precision, so points of one metric less than a microsecond
    apart share a row: the point with the latest nanosecond timestamp is kept.
    """
    rows: dict[tuple[str, str, datetime], dict[str, Any]] = {}
    latest_ns: dict[tuple[str, str, datetime], int] = {}
    for metric in metrics:
        value = metric.get("value")
        if value is None:
            continue
        ts_ns = int(metric["ts"])
        ts = ns_to_datetime(ts_ns)
        key = (metric["check_id"], metric["name"], ts)
        if key in latest_ns and latest_ns[key] > ts_ns:
            continue
        latest_ns[key] = ts_ns
        rows[key] = {
            "series_id": series_id,
            "run_id": run_id,
            "check_id": metric["check_id"],
            "name": metric["name"],
            "ts": ts,
            "value": float(value),
        }
    return list(rows.values())


async def persist_report(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    series_id: uuid.UUID,
    report: core.CheckReport,
    now: datetime,
) -> PersistOutcome:
    """Store the report's metrics and findings for one series in the caller's transaction."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(:series))"),
        {"ns": FINDINGS_LOCK_NAMESPACE, "series": str(series_id)},
    )
    rows = _metric_rows(run_id, series_id, report.metrics)
    if rows:
        stmt = pg_insert(Metric).values(rows)
        # Re-running the same data rewrites the same (series, check, name, ts) points.
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["series_id", "check_id", "name", "ts"],
                set_={"value": stmt.excluded.value, "run_id": stmt.excluded.run_id},
            )
        )
    new = merged = 0
    for incoming in report.findings:
        window = _window(incoming)
        existing = await _best_candidate(
            session, run_id=run_id, series_id=series_id, incoming=incoming, window=window
        )
        if existing is None:
            session.add(
                Finding(
                    org_id=DEFAULT_ORG_ID,
                    workspace_id=DEFAULT_WORKSPACE_ID,
                    series_id=series_id,
                    check_id=incoming.check_id,
                    dimension=incoming.dimension,
                    severity=incoming.severity,
                    window_start=window[0],
                    window_end=window[1],
                    score_impact=incoming.score_impact,
                    summary=incoming.summary,
                    evidence=incoming.evidence,
                    status="open",
                    first_run_id=run_id,
                    last_run_id=run_id,
                    occurrences=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
            new += 1
        else:
            await _merge(session, existing, run_id=run_id, incoming=incoming, window=window, now=now)
            merged += 1
    log.info("findings.persisted", run_id=str(run_id), new=new, merged=merged, metrics=len(rows))
    return PersistOutcome(new=new, merged=merged, metrics=len(rows))


# Reads


def finding_to_dict(finding: Finding) -> dict[str, Any]:
    """The `Finding` response shape of spec 003 (window in ns)."""
    return {
        "id": str(finding.id),
        "check_id": finding.check_id,
        "series_id": str(finding.series_id),
        "dimension": finding.dimension,
        "severity": finding.severity,
        "window": {"start": datetime_to_ns(finding.window_start), "end": datetime_to_ns(finding.window_end)},
        "score_impact": finding.score_impact,
        "summary": finding.summary,
        "evidence": finding.evidence,
        "status": finding.status,
        "status_reason": finding.status_reason,
        "status_at": finding.status_at,
        "first_run_id": None if finding.first_run_id is None else str(finding.first_run_id),
        "last_run_id": None if finding.last_run_id is None else str(finding.last_run_id),
        "occurrences": finding.occurrences,
        "created_at": finding.created_at,
        "updated_at": finding.updated_at,
    }


async def list_findings(
    session: AsyncSession, *, filters: FindingFilter, limit: int, cursor: str | None
) -> tuple[list[Finding], str | None]:
    """Findings newest window first, keyset-paginated on `(window_start desc, id desc)`."""
    stmt = select(Finding).where(Finding.workspace_id == DEFAULT_WORKSPACE_ID)
    if filters.series_id is not None:
        stmt = stmt.where(Finding.series_id == filters.series_id)
    if filters.check_id is not None:
        stmt = stmt.where(Finding.check_id == filters.check_id)
    if filters.severities is not None:
        stmt = stmt.where(Finding.severity.in_(filters.severities))
    if filters.dimension is not None:
        stmt = stmt.where(Finding.dimension == filters.dimension)
    if filters.statuses is not None:
        stmt = stmt.where(Finding.status.in_(filters.statuses))
    if filters.run_id is not None:
        stmt = stmt.where(or_(Finding.first_run_id == filters.run_id, Finding.last_run_id == filters.run_id))
    if filters.since is not None:
        stmt = stmt.where(Finding.window_end > filters.since)
    if filters.until is not None:
        stmt = stmt.where(Finding.window_start < filters.until)
    if cursor:
        window_start, finding_id = decode_keyset(cursor)
        stmt = stmt.where(tuple_(Finding.window_start, Finding.id) < (window_start, finding_id))
    stmt = stmt.order_by(Finding.window_start.desc(), Finding.id.desc()).limit(limit + 1)
    rows = list((await session.execute(stmt)).scalars())
    last = rows[limit - 1] if len(rows) > limit else None
    next_cursor = None if last is None else encode_keyset(last.window_start, last.id)
    return rows[:limit], next_cursor


async def get_finding(
    session: AsyncSession, finding_id: uuid.UUID, *, for_update: bool = False
) -> Finding | None:
    """The finding in the default workspace, or None."""
    stmt = select(Finding).where(Finding.id == finding_id, Finding.workspace_id == DEFAULT_WORKSPACE_ID)
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalars().first()


async def change_status(
    session: AsyncSession, finding_id: uuid.UUID, *, status: str, reason: str | None, now: datetime
) -> Finding:
    """Apply one lifecycle transition; raises FindingNotFoundError or TransitionError."""
    if status not in FINDING_STATUSES:
        raise ValueError(f"unknown status {status!r}")
    finding = await get_finding(session, finding_id, for_update=True)
    if finding is None:
        raise FindingNotFoundError(str(finding_id))
    if status not in TRANSITIONS[finding.status]:
        raise TransitionError(finding.status, status)
    finding.status = status
    finding.status_reason = reason.strip() if reason and reason.strip() else None
    finding.status_at = now
    finding.updated_at = now
    await session.flush()
    log.info("finding.status", finding_id=str(finding_id), status=status)
    return finding


async def list_metrics(
    session: AsyncSession,
    series_id: uuid.UUID,
    *,
    name: str | None,
    since: datetime | None,
    until: datetime | None,
    limit: int,
) -> Sequence[Metric]:
    """Metric points of one series, newest first, within `[since, until)`."""
    stmt = select(Metric).where(Metric.series_id == series_id)
    if name is not None:
        stmt = stmt.where(Metric.name == name)
    if since is not None:
        stmt = stmt.where(Metric.ts >= since)
    if until is not None:
        stmt = stmt.where(Metric.ts < until)
    stmt = stmt.order_by(Metric.ts.desc(), Metric.check_id, Metric.name).limit(limit)
    return (await session.execute(stmt)).scalars().all()


async def list_scores(
    session: AsyncSession, series_id: uuid.UUID, *, limit: int, run_id: uuid.UUID | None = None
) -> Sequence[Score]:
    """Score rows of one series, newest first; only the given run's rows when `run_id` is set."""
    stmt = select(Score).where(Score.series_id == series_id)
    if run_id is not None:
        stmt = stmt.where(Score.run_id == run_id)
    stmt = stmt.order_by(Score.computed_at.desc()).limit(limit)
    return (await session.execute(stmt)).scalars().all()
