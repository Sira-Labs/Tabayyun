"""Corrections (spec 035): propose a repair of a window of a series, compute it as the next
version of the corrected layer, approve or reject it.

The raw layer is only read here (ADR-0010). A correction's rows live in the cache layer
`corrected/v{version}` and hold the repaired samples inside its window; the corrected layer at
a version is raw with the windows of the approved corrections up to it laid over, in version
order (`overlay`). No `coverage` is recorded for corrected versions: the `corrections` table
says what each one holds.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import structlog
import tabayyun_core
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tabayyun.authz.scope import Scope
from tabayyun.db.models import CORRECTION_STATUSES, CorrectedRange, Correction, Finding, Series, User
from tabayyun.jobs.names import APPLY_REPAIR_TASK, RUNS_QUEUE
from tabayyun.services import series as series_service
from tabayyun.services import series_chart
from tabayyun.services.cache import LAYER_COLUMNS, RAW_LAYER, CacheError, RunCache, corrected_layer
from tabayyun.services.runs import DEFER_JOB_SQL
from tabayyun.services.timeconv import CORE_NS_MAX, CORE_NS_MIN, to_core_ns

log = structlog.get_logger()

DAY_NS = 86_400 * 10**9
# `impute.like_day`'s default `days` (spec 034).
LIKE_DAY_DEFAULT_DAYS = 4
MAX_REASON = 2000
MAX_METHOD = 64
MAX_RANGES = 1000
WINDOW_TOO_LARGE = "window too large"
BASE_CHANGED = "the corrected layer changed while this was computed; propose it again"
_INT = re.compile(r"^-?\d+$")


class CorrectionError(Exception):
    """A request about a correction that is refused; `code` names why."""

    code = "invalid"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.code)


class InvalidCorrectionError(CorrectionError):
    """The proposal's operation, window or reason is refused; the message says which."""


class FindingNotFoundError(CorrectionError):
    """The proposal names a finding that is not about its series."""

    code = "finding_not_found"


class NotComputedError(CorrectionError):
    """Only a computed correction can be approved."""

    code = "not_computed"


class StaleError(CorrectionError):
    """An overlapping correction was approved after this one was computed."""

    code = "stale"


class AlreadyDecidedError(CorrectionError):
    """Only a proposed or computed correction can be rejected."""

    code = "already_decided"


class _FailedError(Exception):
    """A computation that ends as `failed`; `str()` is the line stored on the correction."""


@dataclass(frozen=True)
class Layered:
    """The window and cache layer of one approved correction."""

    version: int
    start_ns: int
    end_ns: int


@dataclass(frozen=True)
class Proposal:
    """A validated proposal: the operation, its window and why."""

    method: str
    params: dict[str, Any]
    start_ns: int
    end_ns: int
    reason: str
    finding_id: uuid.UUID | None = None


# Pure helpers


def parse_ns(value: object, name: str) -> int:
    """An integer or a decimal string as ns in the core's range.

    Raises:
        InvalidCorrectionError: anything else.
    """
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise InvalidCorrectionError(f"{name} must be nanoseconds as an integer or a decimal string")
    if isinstance(value, str) and not _INT.match(value.strip()):
        raise InvalidCorrectionError(f"{name} must be nanoseconds as an integer or a decimal string")
    parsed = int(value)
    if not CORE_NS_MIN <= parsed <= CORE_NS_MAX:
        raise InvalidCorrectionError(f"{name} is outside the 64-bit nanosecond range")
    return parsed


def params_in(params: Mapping[str, Any]) -> dict[str, Any]:
    """Parameters as stored and as the core takes them: `*_ns` values become integers.

    Raises:
        InvalidCorrectionError: `method` among the parameters, or a `*_ns` value that is not ns.
    """
    if "method" in params:
        raise InvalidCorrectionError("params must not name the method")
    return {k: parse_ns(v, k) if k.endswith("_ns") and v is not None else v for k, v in params.items()}


def params_out(params: Mapping[str, Any]) -> dict[str, Any]:
    """Parameters as the API returns them: `*_ns` integers become strings."""
    return {
        k: str(v) if k.endswith("_ns") and isinstance(v, int) and not isinstance(v, bool) else v
        for k, v in params.items()
    }


def operation(method: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """The core's JSON form of the operation."""
    return {"method": method, **params}


def series_meta(series: Series) -> dict[str, Any]:
    """The core's metadata of a stored series (interval, physical limits, kind)."""
    values = series_service.merged_for_run(series, {})
    return series_service.meta_for_core(str(series.id), values).model_dump(exclude_none=True)


def context_margin_ns(method: str, params: Mapping[str, Any], interval_ns: int | None) -> int:
    """How far around its window an operation looks (spec 035, behaviour 2).

    `impute.linear`: `max_gap_ns`, or one day without it. `impute.like_day`: `days` reference
    days (weeks with `same_weekday`) plus one interval. The others: nothing.
    """
    if method == "impute.linear":
        max_gap = params.get("max_gap_ns")
        return int(max_gap) if max_gap is not None else DAY_NS
    if method == "impute.like_day":
        days = params.get("days")
        step = 7 * DAY_NS if params.get("same_weekday") else DAY_NS
        return int(LIKE_DAY_DEFAULT_DAYS if days is None else days) * step + (interval_ns or 0)
    return 0


def _plain(batch: pa.RecordBatch | None) -> pa.Table | None:
    """The batch's `ts`, `value`, `quality` as a table; None for no rows."""
    if batch is None or batch.num_rows == 0:
        return None
    return pa.Table.from_batches([batch.select(list(LAYER_COLUMNS))])


def _ts_scalar(table: pa.Table, ns: int) -> pa.Scalar:
    return pa.scalar(ns, pa.int64()).cast(table.schema.field("ts").type)


def overlay(
    base: pa.RecordBatch | None, layers: Sequence[tuple[int, int, pa.RecordBatch | None]]
) -> pa.RecordBatch | None:
    """`base` with, for each `(start_ns, end_ns, rows)` in order, its rows of `[start_ns,
    end_ns)` replaced by `rows` (spec 035, behaviour 3).

    `base` and every `rows` are sorted by time and `rows` lie inside their window, so the
    result is sorted and samples that share a timestamp keep their order. None for no rows.
    """
    current = _plain(base)
    for start_ns, end_ns, rows in layers:
        parts: list[pa.Table] = []
        if current is not None:
            ts = current.column("ts")
            parts.append(current.filter(pc.less(ts, _ts_scalar(current, start_ns))))
        replacement = _plain(rows)
        if replacement is not None:
            parts.append(replacement)
        if current is not None:
            ts = current.column("ts")
            parts.append(current.filter(pc.greater_equal(ts, _ts_scalar(current, end_ns))))
        current = pa.concat_tables(parts) if parts else None
    if current is None or current.num_rows == 0:
        return None
    batches = current.combine_chunks().to_batches()
    return batches[0]


def first_line(exc: BaseException) -> str:
    """One line of an error, for `corrections.error`."""
    lines = str(exc).strip().splitlines()
    return (lines[0] if lines else type(exc).__name__)[:500]


def encode_cursor(version: int) -> str:
    """Opaque cursor naming the last version of a page."""
    return base64.urlsafe_b64encode(f"v{version}".encode()).decode()


def decode_cursor(cursor: str) -> int:
    """Inverse of `encode_cursor`; raises ValueError("invalid cursor") on anything else."""
    try:
        text = base64.urlsafe_b64decode(cursor.encode()).decode()
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError("invalid cursor") from exc
    if not re.fullmatch(r"v\d{1,10}", text):
        raise ValueError("invalid cursor")
    return int(text[1:])


# Validation


def validate(
    series: Series,
    *,
    method: str,
    params: Mapping[str, Any],
    from_: object,
    to: object,
    reason: str,
    finding_id: uuid.UUID | None = None,
) -> Proposal:
    """Check a proposal against the core and the spec's limits.

    Raises:
        InvalidCorrectionError: with the core's message for a refused operation, or which
            field is wrong.
    """
    if not method or len(method) > MAX_METHOD:
        raise InvalidCorrectionError("method is required")
    stored = params_in(params)
    start_ns, end_ns = parse_ns(from_, "from"), parse_ns(to, "to")
    if end_ns <= start_ns:
        raise InvalidCorrectionError("to must be later than from")
    trimmed = reason.strip()
    if not 1 <= len(trimmed) <= MAX_REASON:
        raise InvalidCorrectionError(f"reason must have 1 to {MAX_REASON} characters")
    try:
        tabayyun_core.validate_repair(series_meta(series), operation(method, stored))
    except (ValueError, TypeError) as exc:
        raise InvalidCorrectionError(first_line(exc)) from exc
    return Proposal(
        method=method, params=stored, start_ns=start_ns, end_ns=end_ns, reason=trimmed, finding_id=finding_id
    )


# Reads


async def get_correction(session: AsyncSession, scope: Scope, correction_id: uuid.UUID) -> Correction | None:
    """The correction in the scope's workspace, or None."""
    stmt = select(Correction).where(
        Correction.id == correction_id, Correction.workspace_id == scope.workspace_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_corrections(
    session: AsyncSession,
    series_id: uuid.UUID,
    *,
    status: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[Correction], str | None]:
    """A series' corrections, newest version first, and the cursor of the next page.

    Raises:
        ValueError: an unknown status or a malformed cursor.
    """
    stmt = select(Correction).where(Correction.series_id == series_id)
    if status is not None:
        if status not in CORRECTION_STATUSES:
            raise ValueError("unknown status")
        stmt = stmt.where(Correction.status == status)
    if cursor:
        stmt = stmt.where(Correction.version < decode_cursor(cursor))
    rows = list((await session.execute(stmt.order_by(Correction.version.desc()).limit(limit + 1))).scalars())
    next_cursor = encode_cursor(rows[limit - 1].version) if len(rows) > limit else None
    return rows[:limit], next_cursor


async def list_ranges(session: AsyncSession, correction_id: uuid.UUID) -> list[CorrectedRange]:
    """What a correction touched, in time order."""
    stmt = (
        select(CorrectedRange)
        .where(CorrectedRange.correction_id == correction_id)
        .order_by(CorrectedRange.range_start_ns)
    )
    return list((await session.execute(stmt)).scalars())


async def latest_approved(session: AsyncSession, series_id: uuid.UUID) -> int | None:
    """The series' highest approved version, None when it has none (its layer is then raw)."""
    stmt = select(func.max(Correction.version)).where(
        Correction.series_id == series_id, Correction.status == "approved"
    )
    found: int | None = (await session.execute(stmt)).scalar_one_or_none()
    return found


async def approved_layers(
    session: AsyncSession, series_id: uuid.UUID, version: int, start_ns: int, end_ns: int
) -> list[Layered]:
    """The approved corrections up to `version` whose window touches `[start_ns, end_ns)`, in
    ascending version order."""
    stmt = (
        select(Correction.version, Correction.window_start_ns, Correction.window_end_ns)
        .where(
            Correction.series_id == series_id,
            Correction.status == "approved",
            Correction.version <= version,
            Correction.window_start_ns < end_ns,
            Correction.window_end_ns > start_ns,
        )
        .order_by(Correction.version)
    )
    return [Layered(int(v), int(s), int(e)) for v, s, e in (await session.execute(stmt)).all()]


async def read_composed(
    cache: RunCache, series: Series, layers: Sequence[Layered], start_ns: int, end_ns: int
) -> pa.RecordBatch | None:
    """Raw rows of `[start_ns, end_ns)` with the given corrections laid over, in order.

    Raises:
        CacheError: the store cannot be opened or read.
    """
    source_id, series_id = str(series.source_id), str(series.id)
    raw = await asyncio.to_thread(
        cache.read_layer,
        layer=RAW_LAYER,
        source_id=source_id,
        series_id=series_id,
        start_ns=start_ns,
        end_ns=end_ns,
    )
    parts: list[tuple[int, int, pa.RecordBatch | None]] = []
    for layer in layers:
        lo, hi = max(start_ns, layer.start_ns), min(end_ns, layer.end_ns)
        rows = await asyncio.to_thread(
            cache.read_layer,
            layer=corrected_layer(layer.version),
            source_id=source_id,
            series_id=series_id,
            start_ns=lo,
            end_ns=hi,
        )
        parts.append((lo, hi, rows))
    return overlay(raw, parts)


async def read_layer(
    cache: RunCache, session: AsyncSession, series: Series, version: int | None, start_ns: int, end_ns: int
) -> pa.RecordBatch | None:
    """The series' corrected layer at `version` over `[start_ns, end_ns)` (`ts`, `value`,
    `quality`); `version` None is the raw layer. None when there are no rows.

    Raises:
        CacheError: the store cannot be opened or read.
    """
    layers = [] if version is None else await approved_layers(session, series.id, version, start_ns, end_ns)
    return await read_composed(cache, series, layers, start_ns, end_ns)


# Propose


async def _finding_exists(session: AsyncSession, series_id: uuid.UUID, finding_id: uuid.UUID) -> bool:
    stmt = select(Finding.id).where(Finding.id == finding_id, Finding.series_id == series_id).limit(1)
    return (await session.execute(stmt)).first() is not None


async def propose(
    session: AsyncSession, series: Series, proposal: Proposal, *, user_id: uuid.UUID | None, defer: bool
) -> Correction:
    """Store a proposal as the series' next version and, with `defer`, queue its computation
    in the caller's transaction.

    The series row is locked while the version is allocated, so two proposals never share one.

    Raises:
        FindingNotFoundError: `finding_id` is not a finding about this series.
    """
    if proposal.finding_id is not None and not await _finding_exists(session, series.id, proposal.finding_id):
        raise FindingNotFoundError
    await session.execute(select(Series.id).where(Series.id == series.id).with_for_update())
    highest = await session.scalar(
        select(func.max(Correction.version)).where(Correction.series_id == series.id)
    )
    correction = Correction(
        id=uuid.uuid4(),
        org_id=series.org_id,
        workspace_id=series.workspace_id,
        series_id=series.id,
        version=int(highest or 0) + 1,
        status="proposed",
        method=proposal.method,
        params=proposal.params,
        window_start_ns=proposal.start_ns,
        window_end_ns=proposal.end_ns,
        reason=proposal.reason,
        finding_id=proposal.finding_id,
        stats={},
        proposed_by=user_id,
    )
    session.add(correction)
    await session.flush()
    await session.refresh(correction)
    if defer:
        args = {"correction_id": str(correction.id), "org_id": str(series.org_id)}
        await session.execute(
            DEFER_JOB_SQL, {"queue": RUNS_QUEUE, "task": APPLY_REPAIR_TASK, "args": json.dumps(args)}
        )
    return correction


# Compute


@dataclass(frozen=True)
class _Inputs:
    """What one computation reads, taken in one transaction."""

    series: Series
    method: str
    params: dict[str, Any]
    version: int
    start_ns: int
    end_ns: int
    read_start_ns: int
    read_end_ns: int
    base_version: int | None
    layers: list[Layered]


@dataclass(frozen=True)
class _Result:
    """What the core made of the base rows."""

    stats: dict[str, int]
    ranges: list[dict[str, int]]


async def _fail(factory: async_sessionmaker[AsyncSession], correction_id: uuid.UUID, error: str) -> None:
    """`proposed` → `failed` with one line; a correction decided meanwhile is left alone."""
    async with factory() as session, session.begin():
        correction = await session.get(Correction, correction_id, with_for_update=True)
        if correction is None or correction.status != "proposed":
            return
        correction.status = "failed"
        correction.error = error[:500]
        correction.computed_at = await _db_now(session)
    log.warning("correction.failed", correction_id=str(correction_id), error=error)


async def _db_now(session: AsyncSession) -> datetime:
    """The database's clock now: one clock orders computations and approvals of a series."""
    now: datetime = (await session.execute(select(func.clock_timestamp()))).scalar_one()
    return now


async def _inputs(session: AsyncSession, correction_id: uuid.UUID, max_rows: int) -> _Inputs | str | None:
    """The computation's inputs; None when there is nothing to compute, a line when it fails."""
    correction = await session.get(Correction, correction_id)
    if correction is None or correction.status != "proposed":
        return None
    series = await session.get(Series, correction.series_id)
    if series is None:
        return None
    margin = context_margin_ns(correction.method, correction.params, series.expected_interval_ns)
    read_start = to_core_ns(correction.window_start_ns - margin)
    read_end = to_core_ns(correction.window_end_ns + margin)
    ext = await series_chart.extent(session, series.id)
    try:
        series_chart.check_size(ext, read_start, read_end, max_rows)
    except series_chart.WindowTooLargeError:
        return WINDOW_TOO_LARGE
    base_version = await latest_approved(session, series.id)
    layers = (
        []
        if base_version is None
        else await approved_layers(session, series.id, base_version, read_start, read_end)
    )
    session.expunge(series)
    return _Inputs(
        series=series,
        method=correction.method,
        params=dict(correction.params),
        version=correction.version,
        start_ns=correction.window_start_ns,
        end_ns=correction.window_end_ns,
        read_start_ns=read_start,
        read_end_ns=read_end,
        base_version=base_version,
        layers=layers,
    )


def _repair(inputs: _Inputs, base: pa.RecordBatch | None) -> dict[str, Any]:
    """The core's `repair` on the base rows; blocking."""
    table = base if base is not None else _empty_batch()
    out: dict[str, Any] = tabayyun_core.repair(
        table,
        series_meta(inputs.series),
        inputs.start_ns,
        inputs.end_ns,
        operation(inputs.method, inputs.params),
        quality_col="quality",
        max_ranges=MAX_RANGES,
    )
    return out


def _empty_batch() -> pa.RecordBatch:
    """No rows, in the cache's schema."""
    schema = pa.schema(
        [("ts", pa.timestamp("ns", tz="UTC")), ("value", pa.float64()), ("quality", pa.uint8())]
    )
    return pa.RecordBatch.from_pylist([], schema=schema)


async def _run(cache: RunCache, inputs: _Inputs, max_rows: int) -> _Result:
    """Read the base, repair it and write the window's rows to the version's layer.

    Raises:
        CacheError: the store cannot be read or written.
        _FailedError: the base holds too many rows, or the core refused.
    """
    base = await read_composed(cache, inputs.series, inputs.layers, inputs.read_start_ns, inputs.read_end_ns)
    # Coverage spreads a range's rows evenly; the limit is on what is read.
    if base is not None and base.num_rows > max_rows:
        raise _FailedError(WINDOW_TOO_LARGE)
    try:
        out = await asyncio.to_thread(_repair, inputs, base)
    # The core refuses with ValueError; any other ordinary error of it (a panic surfacing as
    # RuntimeError) must fail this correction, not the worker's job.
    except Exception as exc:  # noqa: BLE001
        raise _FailedError(first_line(exc)) from exc
    window: pa.RecordBatch = out["window"]
    if window.num_rows > 0:
        await asyncio.to_thread(
            cache.write_layer,
            layer=corrected_layer(inputs.version),
            source_id=str(inputs.series.source_id),
            series_id=str(inputs.series.id),
            batch=window,
        )
    stats = {name: int(out[name]) for name in ("rows", "changed", "added", "removed", "unrepaired")}
    return _Result(stats=stats, ranges=[{k: int(v) for k, v in r.items()} for r in out["ranges"]])


async def _store(
    factory: async_sessionmaker[AsyncSession], correction_id: uuid.UUID, inputs: _Inputs, result: _Result
) -> bool:
    """Store the result in one transaction. False when a correction of the series was approved
    since the inputs were read: the base is then another one and the result is not stored."""
    async with factory() as session, session.begin():
        # The series row orders this store and the approvals of the series.
        await session.execute(select(Series.id).where(Series.id == inputs.series.id).with_for_update())
        correction = await session.get(Correction, correction_id, with_for_update=True)
        if correction is None or correction.status != "proposed":
            return True
        base_version = await latest_approved(session, inputs.series.id)
        layers = (
            []
            if base_version is None
            else await approved_layers(
                session, inputs.series.id, base_version, inputs.read_start_ns, inputs.read_end_ns
            )
        )
        if (base_version, layers) != (inputs.base_version, inputs.layers):
            return False
        correction.status = "computed"
        correction.stats = result.stats
        correction.base_version = inputs.base_version
        correction.error = None
        correction.computed_at = await _db_now(session)
        for r in result.ranges:
            session.add(
                CorrectedRange(
                    id=uuid.uuid4(),
                    org_id=correction.org_id,
                    workspace_id=correction.workspace_id,
                    correction_id=correction.id,
                    range_start_ns=r["start_ns"],
                    range_end_ns=r["end_ns"],
                    changed=r["changed"],
                    added=r["added"],
                    removed=r["removed"],
                )
            )
    return True


async def compute(
    factory: async_sessionmaker[AsyncSession], correction_id: uuid.UUID, cache: RunCache, max_rows: int
) -> None:
    """Compute a proposed correction on top of the series' corrected layer and store it as
    `computed`, or as `failed` with one line. `factory` is the org's.

    A correction that is not `proposed` is left alone. When another correction of the series
    is approved while this one is computed, it fails: its rows were built on the older base,
    and a second write to its layer would not take the first one's rows away.
    """
    async with factory() as session, session.begin():
        inputs = await _inputs(session, correction_id, max_rows)
    if inputs is None:
        return
    if isinstance(inputs, str):
        await _fail(factory, correction_id, inputs)
        return
    try:
        result = await _run(cache, inputs, max_rows)
    except (CacheError, _FailedError) as exc:
        await _fail(factory, correction_id, str(exc))
        return
    if not await _store(factory, correction_id, inputs, result):
        await _fail(factory, correction_id, BASE_CHANGED)
        return
    log.info("correction.computed", correction_id=str(correction_id), version=inputs.version)


# Decide


async def _locked(session: AsyncSession, correction: Correction) -> Correction:
    """The correction read again under the series' lock, which orders decisions and stores."""
    await session.execute(select(Series.id).where(Series.id == correction.series_id).with_for_update())
    await session.refresh(correction, with_for_update=True)
    return correction


def _note(note: str | None) -> str | None:
    trimmed = (note or "").strip()
    return trimmed[:MAX_REASON] or None


async def approve(
    session: AsyncSession, correction: Correction, *, user_id: uuid.UUID | None, note: str | None
) -> Correction:
    """`computed` → `approved`: the version becomes part of the series' corrected layer.

    Raises:
        NotComputedError: the correction is in any other status.
        StaleError: an overlapping correction of the series was approved after this one was
            computed, so its rows were built without that one.
    """
    await _locked(session, correction)
    if correction.status != "computed":
        raise NotComputedError
    newer = await session.scalar(
        select(Correction.version)
        .where(
            Correction.series_id == correction.series_id,
            Correction.id != correction.id,
            Correction.status == "approved",
            Correction.window_start_ns < correction.window_end_ns,
            Correction.window_end_ns > correction.window_start_ns,
            Correction.decided_at > correction.computed_at,
        )
        .limit(1)
    )
    if newer is not None:
        raise StaleError(f"version {newer} was approved after this one was computed")
    return await _decide(session, correction, "approved", user_id, note)


async def reject(
    session: AsyncSession, correction: Correction, *, user_id: uuid.UUID | None, note: str | None
) -> Correction:
    """`proposed` or `computed` → `rejected`: the version is never part of the layer.

    Raises:
        AlreadyDecidedError: the correction is in any other status.
    """
    await _locked(session, correction)
    if correction.status not in ("proposed", "computed"):
        raise AlreadyDecidedError
    return await _decide(session, correction, "rejected", user_id, note)


async def _decide(
    session: AsyncSession, correction: Correction, status: str, user_id: uuid.UUID | None, note: str | None
) -> Correction:
    correction.status = status
    correction.decided_by = user_id
    correction.decision_note = _note(note)
    correction.decided_at = await _db_now(session)
    await session.flush()
    return correction


# Serialisation


async def user_names(session: AsyncSession, corrections: Sequence[Correction]) -> dict[uuid.UUID, str]:
    """Display names of the people who proposed or decided the corrections."""
    ids = {u for c in corrections for u in (c.proposed_by, c.decided_by) if u is not None}
    if not ids:
        return {}
    rows = await session.execute(select(User.id, User.display_name).where(User.id.in_(ids)))
    return {user_id: name for user_id, name in rows.all()}


def _person(user_id: uuid.UUID | None, names: Mapping[uuid.UUID, str]) -> dict[str, Any] | None:
    return None if user_id is None else {"id": str(user_id), "name": names.get(user_id)}


def to_dict(
    correction: Correction, names: Mapping[uuid.UUID, str], ranges: Sequence[CorrectedRange] | None = None
) -> dict[str, Any]:
    """The API's form of a correction; with `ranges` for the single-correction answer."""
    out: dict[str, Any] = {
        "id": str(correction.id),
        "series_id": str(correction.series_id),
        "version": correction.version,
        "status": correction.status,
        "method": correction.method,
        "params": params_out(correction.params),
        "window": {"from": str(correction.window_start_ns), "to": str(correction.window_end_ns)},
        "reason": correction.reason,
        "finding_id": None if correction.finding_id is None else str(correction.finding_id),
        "base_version": correction.base_version,
        "stats": correction.stats or None,
        "error": correction.error,
        "proposed_by": _person(correction.proposed_by, names),
        "decided_by": _person(correction.decided_by, names),
        "decision_note": correction.decision_note,
        "created_at": correction.created_at,
        "computed_at": correction.computed_at,
        "decided_at": correction.decided_at,
    }
    if ranges is not None:
        out["ranges"] = [
            {
                "from": str(r.range_start_ns),
                "to": str(r.range_end_ns),
                "changed": r.changed,
                "added": r.added,
                "removed": r.removed,
            }
            for r in ranges
        ]
    return out


def audit_details(correction: Correction, series: Series) -> dict[str, Any]:
    """What an audit event says about a correction: series, version, method and window."""
    return {
        "series_id": str(series.id),
        "series": series.name,
        "version": correction.version,
        "method": correction.method,
        "window": {"from": str(correction.window_start_ns), "to": str(correction.window_end_ns)},
    }
