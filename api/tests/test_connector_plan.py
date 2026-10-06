"""Fetch planning (spec 021): spans, point groups, order, the settle cut-off and pacing."""

from __future__ import annotations

import asyncio
import uuid

from tabayyun.connectors import Limits, PointRef
from tabayyun.connectors.plan import Call, Pacer, plan_calls, settled_end, spans

S = 1_000_000_000
DAY = 86_400 * S


def points(n: int) -> list[PointRef]:
    return [PointRef(uuid.UUID(int=i + 1), f"p{i:03d}") for i in range(n)]


def test_thirty_days_over_250_points_is_15_calls():
    limits = Limits(max_points=100, max_span_ns=7 * DAY, requests_per_second=5)
    calls = plan_calls({p: [(0, 30 * DAY)] for p in points(250)}, limits)
    assert len(calls) == 15
    assert [(c.start_ns, c.end_ns) for c in calls[::3]] == [
        (k * 7 * DAY, min(30, (k + 1) * 7) * DAY) for k in range(5)
    ]
    assert [len(c.points) for c in calls[:3]] == [100, 100, 50]
    assert all(a.start_ns <= b.start_ns for a, b in zip(calls, calls[1:], strict=False))


def test_points_with_different_gaps_get_their_own_calls():
    a, b = points(2)
    limits = Limits(max_points=10, max_span_ns=DAY, requests_per_second=1)
    calls = plan_calls({a: [(0, DAY)], b: [(0, DAY), (3 * DAY, 3 * DAY + 5 * S)]}, limits)
    assert calls == [Call(0, DAY, (a, b)), Call(3 * DAY, 3 * DAY + 5 * S, (b,))]


def test_no_gaps_no_calls():
    assert plan_calls({p: [] for p in points(3)}, Limits(10, DAY, 1)) == []


def test_spans():
    assert spans(0, 10, 4) == [(0, 4), (4, 8), (8, 10)]
    assert spans(5, 5, 4) == []


def test_settled_end():
    assert settled_end(100, now_ns=1000, settle_ns=300) == 100
    assert settled_end(900, now_ns=1000, settle_ns=300) == 700


def test_pacer_spaces_calls_by_the_rate():
    now = [0.0]
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    pacer = Pacer(4.0, clock=lambda: now[0], sleep=sleep)

    async def go():
        starts = []
        for _ in range(5):
            await pacer.wait()
            starts.append(now[0])
            now[0] += 0.05  # each call takes 50 ms
        return starts

    starts = asyncio.run(go())
    assert starts == [0.0, 0.25, 0.5, 0.75, 1.0]
    assert all(abs(s - 0.2) < 1e-9 for s in slept)
    # Five calls at 4 per second take at least a second: never faster than the rate.
    assert (len(starts) - 1) / (starts[-1] - starts[0]) <= 4.0
