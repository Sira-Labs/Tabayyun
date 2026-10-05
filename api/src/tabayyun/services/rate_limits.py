"""Rate limits (spec 015): fixed windows counted in Postgres, so api replicas share them.

One `INSERT … ON CONFLICT DO UPDATE … RETURNING hits` per limited request, in its own short
transaction without a tenant context (`rate_limits` has no RLS). Refused requests count too,
so a client that keeps hammering stays refused until its window ends. A failure to count lets
the request through: a broken counter must not lock everybody out of sign-in.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tabayyun.db.models import RateLimit

log = structlog.get_logger()

KEEP_FOR = timedelta(days=1)

HIT_SQL = text(
    "INSERT INTO rate_limits (bucket, key, window_start, hits) VALUES (:bucket, :key, :start, 1) "
    "ON CONFLICT (bucket, key, window_start) DO UPDATE SET hits = rate_limits.hits + 1 "
    "RETURNING hits"
)


@dataclass(frozen=True)
class Rule:
    """A bucket: at most `limit` requests per `window` and key."""

    bucket: str
    limit: int
    window: timedelta


MINUTE, HOUR = timedelta(minutes=1), timedelta(hours=1)

RULES: dict[str, Rule] = {
    rule.bucket: rule
    for rule in (
        Rule("auth.login", 20, MINUTE),
        Rule("auth.callback", 20, MINUTE),
        Rule("auth.backchannel", 60, MINUTE),
        Rule("auth.sessions", 30, MINUTE),
        Rule("admin.write", 60, MINUTE),
        Rule("admin.invite", 50, HOUR),
    )
}


@dataclass(frozen=True)
class Decision:
    """The outcome of one counted request."""

    allowed: bool
    hits: int
    retry_after: int


def window_start(now: datetime, window: timedelta) -> datetime:
    """The start of the fixed window holding `now` (windows align to the epoch, in UTC)."""
    seconds = window.total_seconds()
    epoch = now.astimezone(UTC).timestamp()
    return datetime.fromtimestamp(math.floor(epoch / seconds) * seconds, UTC)


def seconds_left(now: datetime, window: timedelta) -> int:
    """Whole seconds until the window holding `now` ends, at least 1 (for `Retry-After`)."""
    end = window_start(now, window) + window
    return max(1, math.ceil((end - now.astimezone(UTC)).total_seconds()))


def utc_now() -> datetime:
    return datetime.now(UTC)


async def hit(factory: async_sessionmaker[AsyncSession], rule: Rule, key: str, now: datetime) -> Decision:
    """Count one request for `key` in `rule`'s current window; allowed while within the limit.

    A database error is logged as `rate.unavailable` and allows the request.
    """
    try:
        async with factory() as session, session.begin():
            hits = int(
                (
                    await session.execute(
                        HIT_SQL, {"bucket": rule.bucket, "key": key, "start": window_start(now, rule.window)}
                    )
                ).scalar_one()
            )
    except (SQLAlchemyError, OSError) as exc:
        log.warning("rate.unavailable", bucket=rule.bucket, error=type(exc).__name__)
        return Decision(allowed=True, hits=0, retry_after=0)
    allowed = hits <= rule.limit
    if not allowed:
        log.info("rate.limited", bucket=rule.bucket, key=key, hits=hits)
    return Decision(allowed=allowed, hits=hits, retry_after=0 if allowed else seconds_left(now, rule.window))


async def prune(factory: async_sessionmaker[AsyncSession], now: datetime | None = None) -> int:
    """Delete windows that started more than a day ago; returns how many."""
    cutoff = (now or utc_now()) - KEEP_FOR
    async with factory() as session, session.begin():
        result = await session.execute(delete(RateLimit).where(RateLimit.window_start < cutoff))
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
