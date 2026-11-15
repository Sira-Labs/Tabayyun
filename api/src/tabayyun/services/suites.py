"""Check suites (spec 027): a dataset, the checks to run on it and optionally a cron schedule.

A suite's run is a dataset run (spec 008) that carries the suite's checks. The scheduler
claims due suites across orgs through `tabayyun_claim_due_suites`, then starts each run in
its org's tenant context.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from croniter import CroniterBadDateError, croniter
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tabayyun import core
from tabayyun.authz.scope import Scope
from tabayyun.db import for_org
from tabayyun.db.models import CheckSuite, Dataset, Run
from tabayyun.services import dataset_runs
from tabayyun.services import datasets as datasets_service
from tabayyun.services import runs as runs_service
from tabayyun.services.pagination import decode_keyset, encode_keyset

log = structlog.get_logger()

MAX_NAME = 256
# Successive firings of a schedule must be at least this far apart.
MIN_INTERVAL = timedelta(minutes=5)
DAY_MINUTES = 24 * 60
CLAIM_DUE_SQL = text("SELECT suite_id, org_id, workspace_id FROM tabayyun_claim_due_suites(:now)")
ACTIVE_STATUSES = ("queued", "running")


class SuiteError(ValueError):
    """A suite field the request must change (422)."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


class SuiteNameTakenError(Exception):
    """Another suite of the workspace has this name (409)."""


class NoDatasetError(Exception):
    """The suite's dataset was deleted, so it cannot run (409)."""


@dataclass(frozen=True)
class Threshold:
    """One parameter of a suite's check and where its value comes from."""

    param: str
    value: Any
    source: str
    note: str | None


def catalogue() -> dict[str, dict[str, Any]]:
    """The core's check catalogue by check id, in catalogue order."""
    return {c["id"]: c for c in core.check_catalogue()}


def default_checks() -> list[dict[str, Any]]:
    """Every built-in check, enabled, without overrides: what a new suite runs."""
    return [{"id": check_id, "enabled": True, "params": {}} for check_id in catalogue()]


def validate_name(name: str) -> str:
    """The trimmed name, 1 to 256 characters."""
    name = name.strip()
    if not 1 <= len(name) <= MAX_NAME:
        raise SuiteError("name", f"must be 1 to {MAX_NAME} characters")
    return name


def validate_schedule(schedule: str | None) -> str | None:
    """A five-field cron expression whose firings are at least five minutes apart, or None."""
    if schedule is None:
        return None
    schedule = " ".join(schedule.split())
    if len(schedule.split(" ")) != 5 or not croniter.is_valid(schedule):
        raise SuiteError("schedule", "must be a cron expression of five fields, for example `5 * * * *`")
    try:
        croniter(schedule, datetime(2026, 1, 1, tzinfo=UTC)).get_next(datetime)
    except CroniterBadDateError as exc:  # fields that parse but name no date, like 31 February
        raise SuiteError("schedule", "never fires: no date matches it") from exc
    if _shortest_gap(schedule) < MIN_INTERVAL:
        raise SuiteError("schedule", "must not fire more often than every 5 minutes")
    return schedule


def _shortest_gap(schedule: str) -> timedelta:
    """The shortest time between two firings of a valid schedule on days it fires.

    Computed from every minute and hour the expression names, not from sampled firings, so a
    close pair late in the day counts. The gap over midnight assumes the next day fires too,
    which errs on the strict side for schedules limited to some days.
    """
    fields = croniter.expand(schedule)[0]
    minutes = range(60) if "*" in fields[0] else [int(m) for m in fields[0]]
    hours = range(24) if "*" in fields[1] else [int(h) for h in fields[1]]
    times = sorted(h * 60 + m for h in hours for m in minutes)
    gaps = [later - earlier for earlier, later in zip(times, times[1:], strict=False)]
    gaps.append(times[0] + DAY_MINUTES - times[-1])
    return timedelta(minutes=min(gaps))


def validate_checks(checks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The checks as stored: known ids, each once, parameters the check has and accepts."""
    known = catalogue()
    if not checks:
        raise SuiteError("checks", "a suite needs at least one check")
    stored: list[dict[str, Any]] = []
    seen: set[str] = set()
    for check in checks:
        check_id = check["id"]
        info = known.get(check_id)
        if info is None:
            raise SuiteError("checks", f"unknown check `{check_id}`")
        if check_id in seen:
            raise SuiteError("checks", f"`{check_id}` is listed twice")
        seen.add(check_id)
        params = check.get("params") or {}
        unknown = sorted(set(params) - set(info["defaults"]))
        if unknown:
            raise SuiteError("checks", f"`{check_id}` has no parameter `{unknown[0]}`")
        stored.append({"id": check_id, "enabled": bool(check.get("enabled", True)), "params": params})
    if not any(c["enabled"] for c in stored):
        # A run with every check off would succeed with no findings and say nothing ran.
        raise SuiteError("checks", "at least one check must be switched on")
    try:
        # The core builds every check, enabled or not: a disabled one may be switched on later.
        core.validate_configs([{"id": c["id"], "params": c["params"] or None} for c in stored])
    except ValueError as exc:
        raise SuiteError("checks", str(exc)) from exc
    return stored


def next_run(schedule: str | None, enabled: bool, now: datetime) -> datetime | None:
    """The first firing of the schedule after `now`, in UTC; None without one or when disabled."""
    if schedule is None or not enabled:
        return None
    fired: datetime = croniter(schedule, now.astimezone(UTC)).get_next(datetime)
    return fired


def thresholds(check: dict[str, Any], info: dict[str, Any]) -> list[Threshold]:
    """Every parameter of the check with the value that applies and where it comes from."""
    params = check.get("params") or {}
    out = []
    for param, default in info["defaults"].items():
        auto = info["auto"].get(param)
        # A null for a parameter decided at run time is not an override: the core falls back
        # to the metadata or baseline all the same.
        if param in params and not (auto is not None and params[param] is None):
            out.append(Threshold(param, params[param], "override", None))
        elif auto is not None and default is None:
            out.append(Threshold(param, None, auto["source"], auto["note"]))
        else:
            out.append(Threshold(param, default, "default", None))
    return out


async def _dataset_or_error(session: AsyncSession, scope: Scope, dataset_id: uuid.UUID) -> Dataset:
    dataset = await datasets_service.get_dataset(session, scope, dataset_id)
    if dataset is None:
        raise SuiteError("dataset_id", "no such dataset in this workspace")
    return dataset


async def _flush_or_conflict(session: AsyncSession) -> None:
    """Flush; a unique violation on the name (a concurrent write) becomes a 409."""
    try:
        await session.flush()
    except IntegrityError as exc:
        if "uq_check_suites_workspace_id_name" in str(exc.orig):
            raise SuiteNameTakenError from exc
        raise


async def _name_taken(
    session: AsyncSession, scope: Scope, name: str, *, but: uuid.UUID | None = None
) -> bool:
    stmt = select(CheckSuite.id).where(CheckSuite.workspace_id == scope.workspace_id, CheckSuite.name == name)
    if but is not None:
        stmt = stmt.where(CheckSuite.id != but)
    return (await session.execute(stmt)).first() is not None


async def create_suite(
    session: AsyncSession,
    scope: Scope,
    *,
    name: str,
    dataset_id: uuid.UUID,
    schedule: str | None,
    enabled: bool,
    checks: Sequence[dict[str, Any]] | None,
    now: datetime,
) -> CheckSuite:
    """Validate and store a new suite in the caller's transaction.

    Raises SuiteError (422) or SuiteNameTakenError (409).
    """
    name = validate_name(name)
    schedule = validate_schedule(schedule)
    stored = default_checks() if checks is None else validate_checks(checks)
    await _dataset_or_error(session, scope, dataset_id)
    if await _name_taken(session, scope, name):
        raise SuiteNameTakenError
    suite = CheckSuite(
        org_id=scope.org_id,
        workspace_id=scope.workspace_id,
        name=name,
        dataset_id=dataset_id,
        schedule=schedule,
        enabled=enabled,
        checks=stored,
        next_run_at=next_run(schedule, enabled, now),
        created_at=now,
        updated_at=now,
    )
    session.add(suite)
    await _flush_or_conflict(session)
    log.info("suite.created", suite_id=str(suite.id), schedule=schedule, n_checks=len(stored))
    return suite


async def get_suite(session: AsyncSession, scope: Scope, suite_id: uuid.UUID) -> CheckSuite | None:
    """One suite of the workspace, or None."""
    suite = await session.get(CheckSuite, suite_id)
    return suite if suite is not None and suite.workspace_id == scope.workspace_id else None


async def list_suites(
    session: AsyncSession, scope: Scope, *, limit: int, cursor: str | None
) -> tuple[list[CheckSuite], str | None]:
    """Suites newest first; keyset pagination."""
    stmt = select(CheckSuite).where(CheckSuite.workspace_id == scope.workspace_id)
    if cursor is not None:
        ts, last_id = decode_keyset(cursor)
        stmt = stmt.where(
            (CheckSuite.created_at < ts) | ((CheckSuite.created_at == ts) & (CheckSuite.id < last_id))
        )
    stmt = stmt.order_by(CheckSuite.created_at.desc(), CheckSuite.id.desc()).limit(limit + 1)
    rows = list((await session.execute(stmt)).scalars())
    next_cursor = encode_keyset(rows[limit - 1].created_at, rows[limit - 1].id) if len(rows) > limit else None
    return rows[:limit], next_cursor


async def patch_suite(
    session: AsyncSession, scope: Scope, suite: CheckSuite, changes: dict[str, Any], *, now: datetime
) -> dict[str, Any]:
    """Apply the fields the request sent (`changes` holds only those). Returns what changed,
    as `{field: [before, after]}`, for the audit log.

    Raises SuiteError (422) or SuiteNameTakenError (409).
    """
    before = {f: getattr(suite, f) for f in ("name", "dataset_id", "schedule", "enabled", "checks")}
    if "name" in changes:
        name = validate_name(changes["name"])
        if await _name_taken(session, scope, name, but=suite.id):
            raise SuiteNameTakenError
        suite.name = name
    if "dataset_id" in changes:
        suite.dataset_id = (await _dataset_or_error(session, scope, changes["dataset_id"])).id
    if "schedule" in changes:
        suite.schedule = validate_schedule(changes["schedule"])
    if "enabled" in changes:
        suite.enabled = bool(changes["enabled"])
    if "checks" in changes:
        suite.checks = validate_checks(changes["checks"])
    if before["schedule"] != suite.schedule or before["enabled"] != suite.enabled:
        suite.next_run_at = next_run(suite.schedule, suite.enabled, now)
    suite.updated_at = now
    await _flush_or_conflict(session)
    changed = {}
    for field, old in before.items():
        new = getattr(suite, field)
        if old != new:
            # The audit log keeps ids as strings and does not repeat the whole check list.
            pair = ["changed", "changed"] if field == "checks" else [_plain(old), _plain(new)]
            changed[field] = pair
    return changed


def _plain(value: Any) -> Any:
    return str(value) if isinstance(value, uuid.UUID) else value


async def delete_suite(session: AsyncSession, suite: CheckSuite) -> None:
    """Delete the suite; its runs stay with `suite_id` null."""
    await session.delete(suite)
    await session.flush()


async def start_run(session: AsyncSession, scope: Scope, suite: CheckSuite, *, now: datetime) -> Run:
    """Queue a run of the suite over its dataset's window at `now`, in the caller's transaction.

    Raises NoDatasetError, or DatasetError when the window lies outside the core's range.
    """
    if suite.dataset_id is None:
        raise NoDatasetError
    try:
        run = await dataset_runs.create_dataset_run(
            session, scope, suite.dataset_id, now=now, suite_id=suite.id, checks=list(suite.checks)
        )
    except dataset_runs.DatasetNotFoundError as exc:
        raise NoDatasetError from exc
    suite.last_run_at = now
    return run


async def last_runs(session: AsyncSession, suite_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Run]:
    """The newest run of each suite that has one."""
    if not suite_ids:
        return {}
    stmt = (
        select(Run)
        .where(Run.suite_id.in_(suite_ids))
        .ext(distinct_on(Run.suite_id))
        .order_by(Run.suite_id, Run.created_at.desc(), Run.id.desc())
    )
    return {run.suite_id: run for run in (await session.execute(stmt)).scalars() if run.suite_id is not None}


async def dataset_names(session: AsyncSession, dataset_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Names of the datasets, by id."""
    if not dataset_ids:
        return {}
    rows = await session.execute(select(Dataset.id, Dataset.name).where(Dataset.id.in_(dataset_ids)))
    return {dataset_id: name for dataset_id, name in rows}


# The scheduler (worker path)


async def claim_due_suites(
    factory: async_sessionmaker[AsyncSession], now: datetime
) -> list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]]:
    """Suites whose schedule is due, with a ten-minute lease: (suite, org, workspace)."""
    async with factory() as session, session.begin():
        rows = await session.execute(CLAIM_DUE_SQL, {"now": now})
        return [(r.suite_id, r.org_id, r.workspace_id) for r in rows]


async def fire_suite(
    factory: async_sessionmaker[AsyncSession], suite_id: uuid.UUID, now: datetime
) -> uuid.UUID | None:
    """Start the run of one claimed suite (in its org's factory) and set its next firing.

    Returns the run's id, or None when no run was started: the previous run of the suite has
    not finished, or the suite changed since the claim. Either way the next firing is the
    first one after `now`, so downtime gives one run and not one per missed firing.
    """
    async with factory() as session, session.begin():
        suite = await session.get(CheckSuite, suite_id, with_for_update=True)
        if suite is None:
            return None
        suite.next_run_at = next_run(suite.schedule, suite.enabled, now)
        if suite.next_run_at is None or suite.dataset_id is None:
            return None
        busy = select(Run.id).where(Run.suite_id == suite.id, Run.status.in_(ACTIVE_STATUSES)).limit(1)
        if (await session.execute(busy)).first() is not None:
            log.info("suite.skipped_busy", suite_id=str(suite.id))
            return None
        scope = Scope(org_id=suite.org_id, workspace_id=suite.workspace_id)
        try:
            run = await start_run(session, scope, suite, now=now)
        except (NoDatasetError, datasets_service.DatasetError) as exc:
            log.warning("suite.not_started", suite_id=str(suite.id), reason=type(exc).__name__)
            return None
        await runs_service.enqueue_run(session, run.id, suite.org_id)
        log.info("suite.fired", suite_id=str(suite.id), run_id=str(run.id))
        return run.id


async def fire_due_suites(factory: async_sessionmaker[AsyncSession], now: datetime) -> list[uuid.UUID]:
    """One scheduler tick: start a run for every due suite. Returns the runs started."""
    started = []
    for suite_id, org_id, _workspace in await claim_due_suites(factory, now):
        run_id = await fire_suite(for_org(factory, org_id), suite_id, now)
        if run_id is not None:
            started.append(run_id)
    return started
