"""The process's ear on the event channel (spec 030): one database connection that listens
and hands every event to the broker, and connects again when it is lost."""

from __future__ import annotations

import asyncio

import psycopg
import structlog

from tabayyun.events import CHANNEL, Event, MalformedEventError
from tabayyun.events.broker import Broker

log = structlog.get_logger()

# Pause before connecting again: doubled after each failure, up to the maximum.
BACKOFF_FIRST_S = 1.0
BACKOFF_MAX_S = 30.0
APPLICATION_NAME = "tabayyun-events"


class Listener:
    """Listens on the channel for one `Broker`. `ensure_started` is called with every new
    stream; the task runs until `stop`."""

    def __init__(self, conninfo: str, broker: Broker, *, backoff_first_s: float = BACKOFF_FIRST_S) -> None:
        self._conninfo = conninfo
        self._broker = broker
        self._backoff_first_s = backoff_first_s
        self._task: asyncio.Task[None] | None = None
        self._listening = asyncio.Event()

    def ensure_started(self) -> None:
        """Start listening unless it already does."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="tabayyun-events-listener")

    async def listening(self, timeout_s: float) -> bool:
        """Wait until the channel is being listened on; False after `timeout_s`."""
        try:
            await asyncio.wait_for(self._listening.wait(), timeout_s)
        except TimeoutError:
            return False
        return True

    async def stop(self) -> None:
        """Stop listening and close the connection."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        """Listen until cancelled; after a lost connection, connect again and tell every
        stream to reload, since notifications sent in between are gone."""
        backoff = self._backoff_first_s
        first = True
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(
                    self._conninfo, autocommit=True, application_name=APPLICATION_NAME
                ) as conn:
                    # The channel is a constant of this module, not input.
                    await conn.execute(f"LISTEN {CHANNEL}")
                    log.info("events.listening", reconnected=not first)
                    if not first:
                        self._broker.resync_all()
                    first = False
                    backoff = self._backoff_first_s
                    self._listening.set()
                    async for notification in conn.notifies():
                        self._deliver(notification.payload)
            except (psycopg.Error, OSError) as exc:
                log.warning(
                    "events.listener_lost",
                    error=(str(exc).strip() or type(exc).__name__).splitlines()[0],
                    retry_in_s=backoff,
                )
            finally:
                self._listening.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_S)

    def _deliver(self, payload: str) -> None:
        """Hand one notification to the broker; a malformed one is logged and dropped."""
        try:
            event = Event.decode(payload)
        except MalformedEventError as exc:
            log.warning("events.malformed", error=str(exc))
            return
        self._broker.dispatch(event)
