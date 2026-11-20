"""The streams of one API process and what each of them is owed (spec 030)."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field

import structlog

from tabayyun.authz.scope import Scope
from tabayyun.events import RESYNC, Event

log = structlog.get_logger()

# Events one stream holds for a client that reads slowly; over it, the stream is told to
# reload instead.
BUFFER = 100


class TooManyStreamsError(Exception):
    """The process already serves as many streams as it may."""


@dataclass(eq=False)
class Subscription:
    """One stream: the workspace it watches and the events waiting for its client."""

    scope: Scope
    queue: asyncio.Queue[Event] = field(default_factory=lambda: asyncio.Queue(maxsize=BUFFER))

    def resync(self) -> None:
        """Drop what waits and tell the client to reload: events were or may have been missed."""
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put_nowait(
            Event(type=RESYNC, org_id=self.scope.org_id, workspace_id=self.scope.workspace_id)
        )

    def offer(self, event: Event) -> None:
        """Queue `event`; a full queue becomes one `resync`."""
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            log.warning("events.overflow", workspace_id=str(self.scope.workspace_id))
            self.resync()


class Broker:
    """Hands each event to the streams of its org and workspace. Used from one event loop."""

    def __init__(self, max_streams: int) -> None:
        self._max = max_streams
        self._streams: dict[tuple[uuid.UUID, uuid.UUID], set[Subscription]] = {}
        self._count = 0

    @property
    def streams(self) -> int:
        """Streams open now."""
        return self._count

    def subscribe(self, scope: Scope) -> Subscription:
        """A new stream on `scope`; `TooManyStreamsError` at the limit."""
        if self._count >= self._max:
            raise TooManyStreamsError
        subscription = Subscription(scope)
        self._streams.setdefault((scope.org_id, scope.workspace_id), set()).add(subscription)
        self._count += 1
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        """Give the stream's place back; a second call does nothing."""
        key = (subscription.scope.org_id, subscription.scope.workspace_id)
        streams = self._streams.get(key)
        if streams is None or subscription not in streams:
            return
        streams.discard(subscription)
        if not streams:
            del self._streams[key]
        self._count -= 1

    def dispatch(self, event: Event) -> int:
        """Offer `event` to the streams of its workspace; returns how many there were."""
        streams = self._streams.get((event.org_id, event.workspace_id), ())
        for subscription in streams:
            subscription.offer(event)
        return len(streams)

    def resync_all(self) -> None:
        """Tell every stream to reload (the listener was away)."""
        for streams in self._streams.values():
            for subscription in streams:
                subscription.resync()
