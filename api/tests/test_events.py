"""Live events without a database (spec 030): the payload, the broker and the stream."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime

import pytest

from tabayyun.authz.scope import Scope
from tabayyun.events import FINDING_CREATED, RESYNC, RUN_COMPLETED, Event, MalformedEventError
from tabayyun.events.broker import BUFFER, Broker, TooManyStreamsError
from tabayyun.events.stream import KEEP_ALIVE, frame, stream

ORG_A, ORG_B = uuid.uuid4(), uuid.uuid4()
WS_1, WS_2 = uuid.uuid4(), uuid.uuid4()
SCOPE = Scope(org_id=ORG_A, workspace_id=WS_1)


def _event(type_: str = RUN_COMPLETED, org: uuid.UUID = ORG_A, workspace: uuid.UUID = WS_1, **data) -> Event:
    return Event(type=type_, org_id=org, workspace_id=workspace, data=data)


def test_encode_decode_round_trip():
    """What is published is what a listener reads, and it fits a notification."""
    event = Event(
        type=RUN_COMPLETED,
        org_id=ORG_A,
        workspace_id=WS_1,
        data={"run_id": str(uuid.uuid4()), "status": "succeeded", "n_findings_new": 3},
        at=datetime(2026, 11, 19, 22, 41, 7, 123456, tzinfo=UTC),
    )
    payload = event.encode()
    assert len(payload.encode()) < 8000
    assert Event.decode(payload) == event
    # The stream's client gets the type, the time and the data, not the tenant ids.
    assert event.public() == {"type": RUN_COMPLETED, "at": "2026-11-19T22:41:07.123456+00:00", **event.data}


def _payload(**changes) -> str:
    """A valid payload with some fields replaced, or removed when given as `...`."""
    raw = {
        "v": 1,
        "type": "x",
        "org_id": str(ORG_A),
        "workspace_id": str(WS_1),
        "at": "2026-01-01T00:00:00+00:00",
        "data": {},
        **changes,
    }
    return json.dumps({k: v for k, v in raw.items() if v is not ...})


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        _payload(v=2),
        _payload(org_id=...),
        _payload(org_id="no-uuid"),
        _payload(type=7),
        _payload(at="soon"),
        _payload(data=[]),
    ],
)
def test_decode_refuses_malformed(payload):
    with pytest.raises(MalformedEventError):
        Event.decode(payload)


def test_broker_delivers_by_org_and_workspace():
    """A stream gets the events of its own org and workspace and no others."""
    broker = Broker(max_streams=10)
    mine = broker.subscribe(SCOPE)
    same_workspace = broker.subscribe(SCOPE)
    other_workspace = broker.subscribe(Scope(org_id=ORG_A, workspace_id=WS_2))
    other_org = broker.subscribe(Scope(org_id=ORG_B, workspace_id=WS_1))

    assert broker.dispatch(_event(status="succeeded")) == 2
    assert broker.dispatch(_event(workspace=WS_2)) == 1
    assert broker.dispatch(_event(org=uuid.uuid4())) == 0

    assert mine.queue.qsize() == 1
    assert same_workspace.queue.qsize() == 1
    assert other_workspace.queue.get_nowait().workspace_id == WS_2
    assert other_org.queue.empty()


def test_broker_overflow_resyncs():
    """A stream whose client does not read gets one `resync` in place of a full buffer."""
    broker = Broker(max_streams=1)
    slow = broker.subscribe(SCOPE)
    for n in range(BUFFER):
        broker.dispatch(_event(n=n))
    assert slow.queue.qsize() == BUFFER
    broker.dispatch(_event(n=BUFFER))
    assert slow.queue.qsize() == 1
    assert slow.queue.get_nowait().type == RESYNC
    # After that it receives again.
    broker.dispatch(_event(FINDING_CREATED, count=1))
    assert slow.queue.get_nowait().type == FINDING_CREATED


def test_broker_limit_and_release():
    """Over the limit a stream is refused; a stream that ends gives its place back, once."""
    broker = Broker(max_streams=2)
    first, second = broker.subscribe(SCOPE), broker.subscribe(SCOPE)
    with pytest.raises(TooManyStreamsError):
        broker.subscribe(SCOPE)
    broker.unsubscribe(first)
    broker.unsubscribe(first)
    assert broker.streams == 1
    third = broker.subscribe(SCOPE)
    broker.resync_all()
    assert [s.queue.get_nowait().type for s in (second, third)] == [RESYNC, RESYNC]
    assert first.queue.empty()


def test_sse_frames():
    """An event is one frame: its type as the event name, one line of JSON as the data."""
    event = _event(FINDING_CREATED, run_id="r", count=2)
    text = frame(event).decode()
    assert text.startswith("event: finding.created\ndata: {")
    assert text.endswith("}\n\n")
    assert text.count("\n") == 3
    assert json.loads(text.split("data: ", 1)[1]) == event.public()


async def test_stream_formats_events():
    """The stream starts with the retry hint and sends each event as it comes."""
    broker = Broker(max_streams=1)
    subscription = broker.subscribe(SCOPE)
    frames = stream(subscription, heartbeat_s=5, max_age_s=5)
    first = await anext(frames)
    assert first == b"retry: 5000\n\n: connected\n\n"
    broker.dispatch(_event(run_id="r1", status="failed", n_findings_new=0))
    second = (await asyncio.wait_for(anext(frames), 1)).decode()
    assert second.startswith("event: run.completed\n")
    assert json.loads(second.split("data: ", 1)[1])["status"] == "failed"
    await frames.aclose()


async def test_stream_keep_alive_and_max_age():
    """Silence gives keep-alive comments; at its maximum age the stream ends."""
    broker = Broker(max_streams=1)
    subscription = broker.subscribe(SCOPE)
    chunks = [chunk async for chunk in stream(subscription, heartbeat_s=0.02, max_age_s=0.11)]
    assert chunks[0].startswith(b"retry: ")
    assert 2 <= len(chunks[1:]) <= 6
    assert set(chunks[1:]) == {KEEP_ALIVE}
