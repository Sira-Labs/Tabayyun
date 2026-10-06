"""The fetch job wrapper (spec 021): it raises for Procrastinate's retry only on a retryable
outcome before the last attempt; the job lock and queue are set at defer time."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from tabayyun.jobs import runtime, tasks
from tabayyun.services import fetches


def run_job(monkeypatch, outcome, attempts: int) -> None:
    async def execute(factory, deps, fetch_id):
        return outcome

    monkeypatch.setattr(fetches, "execute_fetch", execute)
    monkeypatch.setattr(runtime, "fetch_deps", lambda: None)
    monkeypatch.setattr(runtime, "session_factory", lambda: SimpleNamespace(class_=None, kw={}))
    monkeypatch.setattr(tasks, "for_org", lambda factory, org: factory)
    context = SimpleNamespace(job=SimpleNamespace(attempts=attempts))
    asyncio.run(tasks.fetch_window.func(context, str(uuid.uuid4()), str(uuid.uuid4())))


def test_retryable_outcome_raises_until_the_last_attempt(monkeypatch):
    partial = fetches.FetchOutcome("partial", calls=2, error="historian busy", retryable=True)
    with pytest.raises(fetches.RetryableFetchError, match="historian busy"):
        run_job(monkeypatch, partial, attempts=0)
    run_job(monkeypatch, partial, attempts=tasks.FETCH_ATTEMPTS - 1)  # last: the fetch stays partial


@pytest.mark.parametrize(
    "outcome",
    [fetches.FetchOutcome("succeeded"), fetches.FetchOutcome("failed", error="refused"), None],
)
def test_other_outcomes_do_not_retry(monkeypatch, outcome):
    run_job(monkeypatch, outcome, attempts=0)


def test_task_names_and_retry():
    assert tasks.fetch_window.name == "tabayyun.fetch_window" and tasks.fetch_window.queue == "fetch"
    assert tasks.check_source.queue == "fetch"
    assert tasks.fetch_window.retry_strategy.max_attempts == tasks.FETCH_ATTEMPTS
