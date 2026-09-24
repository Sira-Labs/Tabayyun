"""Dataset runs over cached uploads (spec 008), with the cache in a temporary directory."""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from tabayyun.main import create_app
from tabayyun.services import dataset_runs, execution
from tabayyun.services.timeconv import datetime_to_ns
from tabayyun.settings import Settings

HOUR0 = datetime(2026, 1, 1, tzinfo=UTC)
HOUR_NS = 3600 * 10**9
WINDOW = {"start": HOUR0.isoformat(), "end": (HOUR0 + timedelta(hours=96)).isoformat()}


@pytest.fixture
def app(db_url, fresh_schema, tmp_path):
    """App on a fresh schema with inline jobs and a local cache."""
    fresh_schema("auto")
    return create_app(Settings(env="test", database_url=db_url, inline_jobs=True, cache_url=str(tmp_path)))


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await app.state.engine.dispose()


def _csv(hours: int, *, offset_h: int = 0, phase: float = 0.0) -> bytes:
    rows = ["ts,value"]
    for h in range(hours):
        ts = HOUR0 + timedelta(hours=offset_h + h)
        rows.append(f"{ts.isoformat()},{50 + 10 * math.sin((h + phase) / 24 * 2 * math.pi):.3f}")
    return ("\n".join(rows) + "\n").encode()


async def _upload(client, name: str, csv: bytes) -> str:
    """Upload `csv` as series `name` (written to the cache) and return the series id."""
    r = await client.post("/api/runs", files={"file": ("f.csv", csv, "text/csv")}, data={"series_id": name})
    assert r.status_code == 202, r.text
    run = (await client.get(f"/api/runs/{r.json()['id']}")).json()
    assert run["status"] == "succeeded" and run["stats"]["cache"]["written"], run
    return run["series"][0]["id"]


async def _run(client, dataset_id: str, **extra) -> dict:
    """Start a dataset run and return it once finished (inline jobs)."""
    r = await client.post("/api/runs", json={"dataset_id": dataset_id, **extra})
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "queued"
    return (await client.get(f"/api/runs/{r.json()['id']}")).json()


async def _group(client, name: str, kind: str, members: list[str]) -> str:
    body = {"name": name, "kind": kind, "members": [{"series_id": m} for m in members]}
    r = await client.post("/api/series-groups", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
async def setup(client) -> dict[str, str]:
    """Four cached series in a dataset over four days, and three groups.

    a, b and c hold 72 h from HOUR0; d holds data only after the window; e is outside the
    dataset. g-ab can run, g-ad has a member without data, g-ae a member outside the dataset.
    """
    ids = {
        "a": await _upload(client, "pt-a", _csv(72)),
        "b": await _upload(client, "pt-b", _csv(72, phase=0.5)),
        "c": await _upload(client, "pt-c", _csv(72, phase=3)),
        "d": await _upload(client, "pt-d", _csv(24, offset_h=200)),
        "e": await _upload(client, "pt-e", _csv(72)),
    }
    ids["g-ab"] = await _group(client, "g-ab", "redundant", [ids["a"], ids["b"]])
    ids["g-ad"] = await _group(client, "g-ad", "related", [ids["a"], ids["d"]])
    ids["g-ae"] = await _group(client, "g-ae", "related", [ids["a"], ids["e"]])
    body = {"name": "PT", "series_ids": [ids[k] for k in "abcd"], "window": WINDOW}
    r = await client.post("/api/datasets", json=body)
    assert r.status_code == 201, r.text
    ids["dataset"] = r.json()["id"]
    return ids


async def test_dataset_run_over_cached_uploads(client, setup):
    ids = setup
    run = await _run(client, ids["dataset"], now=WINDOW["end"])
    assert run["status"] == "succeeded", run
    assert (run["trigger"], run["dataset_id"]) == ("manual", ids["dataset"])
    start, end = datetime_to_ns(HOUR0), datetime_to_ns(HOUR0 + timedelta(hours=96))
    assert run["window"] == {"start": start, "end": end}
    stats = run["stats"]
    assert stats["n_series"] == 3 and stats["n_samples"] == 3 * 72
    assert sorted(s["external_id"] for s in run["series"]) == ["pt-a", "pt-b", "pt-c"]
    assert stats["series_skipped"] == [
        {"series_id": ids["d"], "external_id": "pt-d", "reason": "no cached data"}
    ]
    # A write covers [first sample, last sample + 1 ns), stored to the microsecond: a, b and c
    # are covered up to just after hour 71, d not at all inside the window.
    gap = [[start + 71 * HOUR_NS + 1000, end]]
    assert stats["missing"] == {ids["a"]: gap, ids["b"]: gap, ids["c"]: gap, ids["d"]: [[start, end]]}
    assert stats["groups"] == [ids["g-ab"]]
    skipped = {g["group_id"]: (g["reason"], g["missing"]) for g in stats["groups_skipped"]}
    assert skipped == {
        ids["g-ad"]: ("members without data", [ids["d"]]),
        ids["g-ae"]: ("members not in dataset", [ids["e"]]),
    }
    # Completeness is judged against the dataset window: the last 24 h are missing.
    findings = (await client.get("/api/findings", params={"series_id": ids["a"], "status": "all"})).json()[
        "items"
    ]
    completeness = [
        f for f in findings if f["check_id"] == "tby.completeness" and f["last_run_id"] == run["id"]
    ]
    assert completeness, findings
    scores = (await client.get(f"/api/series/{ids['a']}/scores", params={"run_id": run["id"]})).json()[
        "items"
    ]
    assert len(scores) == 1 and scores[0]["overall"] < 100
    assert stats["n_findings"] == stats["n_findings_new"] + stats["n_findings_merged"] > 0


async def test_a_window_past_the_core_range_reaches_its_last_instant(client):
    """A fixed window ending in 2300 runs over the core's end of time (issue #42).

    The series' last sample is `i64::MAX`, the latest instant the core holds; the window is
    clamped to the core's range, so the run reads that sample and reports no gap after it.
    """
    last = "2262-04-11T23:47:16.854775807Z"  # i64::MAX ns
    csv = f"ts,value\n2262-04-11T23:47:16.854775806Z,1.0\n{last},2.0\n".encode()
    series = await _upload(client, "pt-end", csv)
    window = {"start": "2262-04-11T00:00:00Z", "end": "2300-01-01T00:00:00Z"}
    r = await client.post("/api/datasets", json={"name": "End", "series_ids": [series], "window": window})
    assert r.status_code == 201, r.text
    run = await _run(client, r.json()["id"], now=window["end"])
    assert run["status"] == "succeeded", run
    start = datetime_to_ns(datetime(2262, 4, 11, tzinfo=UTC))
    assert run["window"] == {"start": start, "end": 2**63 - 1}
    assert run["stats"]["n_samples"] == 2
    first = (2**63 - 2) // 1000 * 1000  # the first sample, stored to the microsecond
    assert run["stats"]["missing"] == {series: [[start, first]]}


async def test_rerun_merges_instead_of_doubling(client, setup):
    first = await _run(client, setup["dataset"], now=WINDOW["end"])
    second = await _run(client, setup["dataset"], now=WINDOW["end"])
    assert second["status"] == "succeeded"
    assert second["stats"]["n_findings_new"] == 0
    assert (
        second["stats"]["n_findings_merged"] == second["stats"]["n_findings"] == first["stats"]["n_findings"]
    )
    for key in "abc":
        items = (await client.get("/api/findings", params={"series_id": setup[key], "status": "all"})).json()[
            "items"
        ]
        from_datasets = [f for f in items if f["last_run_id"] == second["id"]]
        assert from_datasets and all(f["first_run_id"] != second["id"] for f in from_datasets)


async def test_relative_window_and_deleted_dataset(client, setup):
    body = {"name": "last", "series_ids": [setup["a"]], "window": {"last": "48h"}}
    dataset = (await client.post("/api/datasets", json=body)).json()
    now = HOUR0 + timedelta(hours=60)
    run = await _run(client, dataset["id"], now=str(datetime_to_ns(now)))
    assert run["status"] == "succeeded", run
    assert run["window"] == {"start": datetime_to_ns(now - timedelta(hours=48)), "end": datetime_to_ns(now)}
    assert run["stats"]["n_samples"] == 48 and run["stats"]["missing"] == {}
    assert run["now_ns"] == datetime_to_ns(now)
    # Deleting the dataset keeps the run, with its dataset cleared.
    assert (await client.delete(f"/api/datasets/{dataset['id']}")).status_code == 204
    kept = (await client.get(f"/api/runs/{run['id']}")).json()
    assert kept["status"] == "succeeded" and kept["dataset_id"] is None


async def test_dataset_without_cached_data_fails(client, setup):
    body = {"name": "empty", "series_ids": [setup["d"]], "window": WINDOW}
    dataset = (await client.post("/api/datasets", json=body)).json()
    run = await _run(client, dataset["id"])
    assert (run["status"], run["error"]) == ("failed", "no cached data in window")


async def test_request_errors(client, setup):
    r = await client.post("/api/runs", json={"dataset_id": str(uuid.uuid4())})
    assert r.status_code == 404
    assert (await client.post("/api/runs", json={"dataset": setup["dataset"]})).status_code == 422
    assert (await client.post("/api/runs", json={"dataset_id": "nope"})).status_code == 422
    r = await client.post("/api/runs", json={"dataset_id": setup["dataset"], "now": "someday"})
    assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", "now"]
    r = await client.post("/api/runs", content=b"{", headers={"content-type": "application/json"})
    assert r.status_code == 422
    r = await client.post("/api/runs", data={"series_id": "x"})
    assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", "file"]


async def test_worker_dispatches_dataset_runs(db_url, fresh_schema, tmp_path):
    """Queued (not inline) dataset runs are executed by the job through `execution.execute`."""
    fresh_schema("auto")
    inline = create_app(Settings(env="test", database_url=db_url, inline_jobs=True, cache_url=str(tmp_path)))
    async with AsyncClient(transport=ASGITransport(app=inline), base_url="http://test") as c:
        series_id = await _upload(c, "pt-w", _csv(24))
        body = {"name": "W", "series_ids": [series_id], "window": {"last": "24h"}}
        dataset_id = (await c.post("/api/datasets", json=body)).json()["id"]
    await inline.state.engine.dispose()
    queued = create_app(Settings(env="test", database_url=db_url, inline_jobs=False, cache_url=str(tmp_path)))
    async with AsyncClient(transport=ASGITransport(app=queued), base_url="http://test") as c:
        now = (HOUR0 + timedelta(hours=24)).isoformat()
        r = await c.post("/api/runs", json={"dataset_id": dataset_id, "now": now})
        run_id = uuid.UUID(r.json()["id"])
        assert (await c.get(f"/api/runs/{run_id}")).json()["status"] == "queued"
        await execution.execute(queued.state.session_factory, run_id, queued.state.run_cache)
        run = (await c.get(f"/api/runs/{run_id}")).json()
    await queued.state.engine.dispose()
    assert run["status"] == "succeeded", run
    assert run["stats"]["n_samples"] == 24 and run["trigger"] == "manual"


async def test_planning_error_fails_the_run(client, setup, monkeypatch):
    """An unexpected error while planning fails the run instead of leaving it queued."""

    async def broken(session, run):
        raise RuntimeError("boom")

    monkeypatch.setattr(dataset_runs, "_plan", broken)
    run = await _run(client, setup["dataset"])
    assert (run["status"], run["error"]) == ("failed", "plan error: boom")
