"""After a run: what its results match, gathered into notifications (spec 031)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.alerts.matching import ScoreValues, finding_matches, score_crossing
from tabayyun.authz.scope import Scope
from tabayyun.db.models import AlertNotification, AlertRule, Finding, Run, Score, Series

log = structlog.get_logger()

# Matches a notification keeps as they are told; the rest is only counted.
MAX_ITEMS = 50
# A job for the worker, to run at `scheduled_at`.
DEFER_AT_SQL = text(
    "SELECT procrastinate_defer_jobs_v1(ARRAY[ROW(:queue, :task, 0, NULL, NULL, CAST(:args AS jsonb), "
    "CAST(:scheduled_at AS timestamptz))::procrastinate_job_to_defer_v1])"
)


async def defer_job(
    session: AsyncSession, notification_id: uuid.UUID, org_id: uuid.UUID, scheduled_at: datetime
) -> None:
    """Queue a delivery job for `scheduled_at`, in the caller's transaction."""
    # Imported here: `tabayyun.jobs` loads its tasks, which import this package.
    from tabayyun.jobs.names import DELIVER_ALERT_TASK, MAIL_QUEUE

    args = {"notification_id": str(notification_id), "org_id": str(org_id)}
    await session.execute(
        DEFER_AT_SQL,
        {
            "queue": MAIL_QUEUE,
            "task": DELIVER_ALERT_TASK,
            "args": json.dumps(args),
            "scheduled_at": scheduled_at,
        },
    )


async def defer_delivery(session: AsyncSession, notification: AlertNotification) -> None:
    """Queue the notification's delivery for when it is due, in the caller's transaction."""
    await defer_job(session, notification.id, notification.org_id, notification.due_at)


async def _new_findings(session: AsyncSession, scope: Scope, run_id: uuid.UUID) -> list[dict[str, Any]]:
    """The findings the run stored as new (not the recurrences it merged), as items."""
    rows = await session.execute(
        select(Finding, Series.name, Series.asset_path)
        .join(Series, Series.id == Finding.series_id)
        .where(Finding.first_run_id == run_id, Series.workspace_id == scope.workspace_id)
        .order_by(Finding.window_start, Finding.id)
    )
    return [
        {
            "finding_id": str(finding.id),
            "series_id": str(finding.series_id),
            "series_name": name,
            "asset_path": asset_path,
            "check_id": finding.check_id,
            "dimension": finding.dimension,
            "severity": finding.severity,
            "window_start": finding.window_start.isoformat(),
            "window_end": finding.window_end.isoformat(),
            "summary": finding.summary,
            "run_id": str(run_id),
        }
        for finding, name, asset_path in rows
    ]


async def _scores(
    session: AsyncSession, scope: Scope, run_id: uuid.UUID
) -> list[tuple[dict[str, Any], ScoreValues, ScoreValues | None]]:
    """Each score the run stored, with the series it belongs to and the score before it."""
    current = (
        await session.execute(
            select(Score, Series.name, Series.asset_path)
            .join(Series, Series.id == Score.series_id)
            # Alerts read the raw layer: a corrected layer's score names its run too (spec 036).
            .where(Score.run_id == run_id, Score.layer == "raw", Series.workspace_id == scope.workspace_id)
            .order_by(Series.name, Score.series_id)
        )
    ).all()
    if not current:
        return []
    computed_at = min(score.computed_at for score, _, _ in current)
    before = await session.scalars(
        select(Score)
        .where(
            Score.series_id.in_([score.series_id for score, _, _ in current]),
            Score.computed_at < computed_at,
            Score.layer == "raw",
        )
        .ext(distinct_on(Score.series_id))
        .order_by(Score.series_id, Score.computed_at.desc())
    )
    previous = {score.series_id: ScoreValues(score.overall, score.dimensions) for score in before}
    return [
        (
            {"series_id": str(score.series_id), "series_name": name, "asset_path": asset_path},
            ScoreValues(score.overall, score.dimensions),
            previous.get(score.series_id),
        )
        for score, name, asset_path in current
    ]


def _matches(
    rule: AlertRule,
    findings: list[dict[str, Any]],
    scores: list[tuple[dict[str, Any], ScoreValues, ScoreValues | None]],
    run_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """What of the run's results the rule matches, as items."""
    if rule.kind == "finding":
        return [
            f
            for f in findings
            if finding_matches(
                rule.condition,
                severity=f["severity"],
                check_id=f["check_id"],
                dimension=f["dimension"],
                asset_path=f["asset_path"],
            )
        ]
    items = []
    for series, now, before in scores:
        crossing = score_crossing(rule.condition, now, before)
        if crossing is not None:
            items.append(
                {
                    **series,
                    "metric": crossing.metric,
                    "score": crossing.score,
                    "previous": crossing.previous,
                    "below": crossing.below,
                    "run_id": str(run_id),
                }
            )
    return items


async def gather(
    session: AsyncSession, rule: AlertRule, items: list[dict[str, Any]], now: datetime
) -> AlertNotification | None:
    """Add `items` to the rule's waiting notification, or start a new one that is due at
    once or the throttle after the one before. Returns the notification when it is new."""
    # One evaluation at a time per rule, so two runs do not both start a notification.
    await session.execute(select(AlertRule.id).where(AlertRule.id == rule.id).with_for_update())
    # The waiting notification is looked up by its status, not by a time an evaluation that
    # waited for the lock could have reversed. Two can wait when one that was being sent came
    # back for a retry after the next was started: matches join the one that goes last.
    own = (AlertNotification.rule_id == rule.id, AlertNotification.kind == "alert")
    waiting = await session.scalar(
        select(AlertNotification)
        .where(*own, AlertNotification.status == "pending")
        .order_by(AlertNotification.due_at.desc(), AlertNotification.id)
        .limit(1)
        .with_for_update()
    )
    if waiting is not None:
        waiting.items = [*waiting.items, *items][:MAX_ITEMS]
        waiting.n_matches += len(items)
        return None
    # The throttle counts from the last one that told something: from when it was sent, which
    # can be later than when it was due, or from its due time while it is on its way. A
    # notification that failed or was skipped told nobody and holds nothing back. This is the
    # plan; the delivery checks the throttle again when it takes the notification, because the
    # one before can be sent later than was known here.
    told = func.greatest(
        AlertNotification.due_at, func.coalesce(AlertNotification.sent_at, AlertNotification.due_at)
    )
    last_told = await session.scalar(
        select(func.max(told)).where(*own, AlertNotification.status.in_(("sending", "sent")))
    )
    due_at = now
    if last_told is not None:
        due_at = max(now, last_told + timedelta(minutes=rule.throttle_minutes))
    notification = AlertNotification(
        id=uuid.uuid4(),
        org_id=rule.org_id,
        workspace_id=rule.workspace_id,
        rule_id=rule.id,
        kind="alert",
        status="pending",
        due_at=due_at,
        n_matches=len(items),
        items=items[:MAX_ITEMS],
        created_at=now,
    )
    session.add(notification)
    await session.flush()
    return notification


async def evaluate_run(
    session: AsyncSession, scope: Scope, run_id: uuid.UUID, *, now: datetime, defer: bool
) -> list[AlertNotification]:
    """Evaluate the workspace's enabled rules on what the run stored, in the caller's
    transaction. Returns the notifications it started. With `defer` their delivery is queued
    for the worker; without it only that of the ones due later is, and the caller delivers
    the rest. A run is evaluated once: a second call for it returns nothing."""
    claimed = await session.scalar(
        update(Run)
        .where(
            Run.id == run_id,
            Run.workspace_id == scope.workspace_id,
            Run.status == "succeeded",
            Run.alerts_evaluated_at.is_(None),
        )
        .values(alerts_evaluated_at=now)
        .returning(Run.id)
    )
    if claimed is None:
        return []
    rules = (
        await session.scalars(
            select(AlertRule)
            .where(AlertRule.workspace_id == scope.workspace_id, AlertRule.enabled)
            .order_by(AlertRule.id)
        )
    ).all()
    if not rules:
        return []
    findings = await _new_findings(session, scope, run_id) if any(r.kind == "finding" for r in rules) else []
    scores = await _scores(session, scope, run_id) if any(r.kind == "score" for r in rules) else []
    started = []
    for rule in rules:
        items = _matches(rule, findings, scores, run_id)
        if not items:
            continue
        notification = await gather(session, rule, items, now)
        log.info(
            "alert.matched",
            rule_id=str(rule.id),
            run_id=str(run_id),
            matches=len(items),
            new_notification=notification is not None,
        )
        if notification is not None:
            started.append(notification)
            # One due later is always queued: nothing else would deliver it when it is due.
            if defer or notification.due_at > now:
                await defer_delivery(session, notification)
    return started


def test_item(rule: AlertRule) -> dict[str, Any]:
    """The made-up match of a test notification."""
    series = {
        "series_id": str(uuid.UUID(int=0)),
        "series_name": "Example series",
        "asset_path": "example/asset",
    }
    if rule.kind == "score":
        below = float(rule.condition.get("below", 0))
        return {
            **series,
            "metric": rule.condition.get("metric", "overall"),
            "score": max(below - 5.0, 0.0),
            "previous": min(below + 5.0, 100.0),
            "below": below,
            "run_id": None,
        }
    return {
        "finding_id": None,
        **series,
        "check_id": "tby.example",
        "dimension": "plausibility",
        "severity": rule.condition.get("min_severity", "critical"),
        "window_start": "2026-01-01T00:00:00+00:00",
        "window_end": "2026-01-01T01:00:00+00:00",
        "summary": "This is what a finding's summary looks like.",
        "run_id": None,
    }


async def start_test(
    session: AsyncSession, rule: AlertRule, *, now: datetime, defer: bool
) -> AlertNotification:
    """A test notification of the rule, due at once; it does not count for the throttle."""
    notification = AlertNotification(
        id=uuid.uuid4(),
        org_id=rule.org_id,
        workspace_id=rule.workspace_id,
        rule_id=rule.id,
        kind="test",
        status="pending",
        due_at=now,
        n_matches=1,
        items=[test_item(rule)],
        created_at=now,
    )
    session.add(notification)
    await session.flush()
    if defer:
        await defer_delivery(session, notification)
    return notification
