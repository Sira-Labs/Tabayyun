"""Runs API against the database with inline jobs (spec 002)."""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from synth_csv import faulty_csv
from tabayyun.db.models import Run, Score, Upload
from tabayyun.jobs.names import RUN_CHECKS_TASK, RUNS_QUEUE
from tabayyun.main import create_app
from tabayyun.services import runs as runs_service
from tabayyun.settings import Settings

FAULTS = ["gap", "flatline", "nans", "negative"]


@pytest.fixture
def app(db_url, fresh_schema):
    """App on a fresh schema, executing jobs inline in the request's background task."""
    fresh_schema("auto")
    return create_app(Settings(env="test", database_url=db_url, inline_jobs=True))


@pytest.fixture
async def client(app):
    """HTTP client whose requests complete their background tasks before returning."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await app.state.engine.dispose()


async def _post_run(client, csv: bytes, **form: str) -> dict:
    data = {"series_id": "demo", "unit": "m3/h", "quality_col": "quality", **form}
    r = await client.post("/api/runs", files={"file": ("f.csv", csv, "text/csv")}, data=data)
    assert r.status_code == 202, r.text
    return r.json()


async def test_post_run_returns_202_and_queued(client):
    """The response carries the id and the queued status, before the job ran."""
    body = await _post_run(client, faulty_csv(FAULTS))
    assert body["status"] == "queued"
    uuid.UUID(body["id"])
    assert body["created_at"]


async def test_run_succeeds_inline_and_matches_stateless_endpoint(client):
    """After the inline job the run is succeeded with the same numbers as /api/checks/run."""
    csv = faulty_csv(FAULTS)
    stateless = await client.post(
        "/api/checks/run",
        files={"file": ("f.csv", csv, "text/csv")},
        data={"series_id": "demo", "unit": "m3/h", "quality_col": "quality"},
    )
    report = stateless.json()
    body = await _post_run(client, csv)
    r = await client.get(f"/api/runs/{body['id']}")
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["status"] == "succeeded", run
    assert run["error"] is None
    assert run["stats"]["n_samples"] == report["n_samples"]
    assert run["stats"]["n_findings"] == len(report["findings"])
    assert run["stats"]["n_metrics"] == len(report["metrics"])
    assert run["stats"]["skipped"] == len(report["skipped"])
    assert run["window"] == report["window"]
    assert run["now_ns"] == report["now_ns"]
    assert run["duration_ms"] is not None and run["duration_ms"] >= 0
    assert run["series"][0]["external_id"] == "demo"
    assert run["series"][0]["score"] == report["score"]["overall"]
    assert run["started_at"] and run["finished_at"]


async def test_score_row_persisted(client, app):
    """The worker writes one raw-layer score row for the series."""
    body = await _post_run(client, faulty_csv(FAULTS))
    async with app.state.session_factory() as session:
        scores = list(
            (await session.execute(select(Score).where(Score.run_id == uuid.UUID(body["id"])))).scalars()
        )
    assert len(scores) == 1
    assert scores[0].layer == "raw"
    assert 0 < scores[0].overall < 100


async def test_unparsable_csv_is_422_in_the_request(client):
    """Parsing happens in the request: a bad file never becomes a run."""
    r = await client.post(
        "/api/runs", files={"file": ("f.csv", b"a,b\n1,2\n", "text/csv")}, data={"series_id": "demo"}
    )
    assert r.status_code == 422
    assert "cannot parse CSV" in r.json()["detail"]
    assert (await client.get("/api/runs")).json()["items"] == []


async def test_run_failed_on_core_error_has_message_not_traceback(client, monkeypatch):
    """An exception in the core marks the run failed with a one-line message."""

    def boom(*args, **kwargs):
        raise RuntimeError("value column exploded\nTraceback (most recent call last): ...")

    monkeypatch.setattr(runs_service.core, "run_checks", boom)
    body = await _post_run(client, faulty_csv(FAULTS))
    run = (await client.get(f"/api/runs/{body['id']}")).json()
    assert run["status"] == "failed"
    assert run["error"] == "core error: value column exploded"
    assert "Traceback" not in run["error"]
    assert run["finished_at"]


async def test_list_runs_pagination(client):
    """Newest first, stable non-overlapping pages through the cursor."""
    ids = [(await _post_run(client, faulty_csv([])))["id"] for _ in range(3)]
    page1 = (await client.get("/api/runs", params={"limit": 2})).json()
    assert [r["id"] for r in page1["items"]] == ids[::-1][:2]
    assert page1["next_cursor"]
    page2 = (await client.get("/api/runs", params={"limit": 2, "cursor": page1["next_cursor"]})).json()
    assert [r["id"] for r in page2["items"]] == [ids[0]]
    assert page2["next_cursor"] is None
    assert (await client.get("/api/runs", params={"cursor": "garbage"})).status_code == 422


async def test_get_run_unknown_404(client):
    """Unknown ids and non-UUIDs are 404."""
    assert (await client.get(f"/api/runs/{uuid.uuid4()}")).status_code == 404
    assert (await client.get("/api/runs/not-a-uuid")).status_code == 404


async def test_upload_deleted_after_terminal_state(client, app):
    """The raw bytes are dropped once the run is terminal; the run keeps its stats."""
    body = await _post_run(client, faulty_csv(FAULTS))
    async with app.state.session_factory() as session:
        assert await session.get(Upload, uuid.UUID(body["id"])) is None
        run = await session.get(Run, uuid.UUID(body["id"]))
    assert run is not None and run.status == "succeeded" and run.stats["n_samples"] > 0


async def test_enqueue_is_transactional_with_the_run(db_url, fresh_schema):
    """Without inline jobs the run row and the Procrastinate job land in one transaction."""
    fresh_schema("auto")
    app = create_app(Settings(env="test", database_url=db_url, inline_jobs=False))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            body = await _post_run(c, faulty_csv([]))
            run = (await c.get(f"/api/runs/{body['id']}")).json()
        assert run["status"] == "queued"
        async with app.state.session_factory() as session:
            jobs = (
                await session.execute(
                    text("SELECT queue_name, task_name, args, status::text FROM procrastinate_jobs")
                )
            ).all()
        assert len(jobs) == 1
        queue, task, args, status = jobs[0]
        assert (queue, task, status) == (RUNS_QUEUE, RUN_CHECKS_TASK, "todo")
        assert args == {"run_id": body["id"]}
    finally:
        await app.state.engine.dispose()


async def test_healthz_reports_queue_depth(client):
    """/healthz counts pending and running jobs when the queue schema exists."""
    body = (await client.get("/healthz")).json()
    assert body["db"] == "ok"
    assert body["queue"] == {"pending": 0, "running": 0}
