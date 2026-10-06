"""Dataset runs fill their gaps from connectors (spec 021): an inline fetch inside the run, errors
in `stats.fetch_errors` without failing the run, and the budget turned off."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from tabayyun import connectors
from tabayyun.connectors import ConnectorError
from tabayyun.connectors.synthetic import SyntheticConnector
from tabayyun.main import create_app
from tenancy import app_settings

DAY0 = datetime(2026, 9, 1, tzinfo=UTC)
WINDOW = {"start": DAY0.isoformat(), "end": (DAY0 + timedelta(days=1)).isoformat()}
POINTS = [
    {"external_id": "inlet", "base": 100, "amplitude": 5, "faults": ["flatline"]},
    {"external_id": "outlet", "base": 98, "amplitude": 5},
]


def make_client(db_url, tmp_path, **settings):
    app = create_app(app_settings(db_url, inline_jobs=True, cache_url=str(tmp_path), **settings))
    return app, AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"X-Tabayyun-Request": "1"}
    )


async def prepare(client) -> tuple[str, list[str], str]:
    """A synthetic source with two series in a dataset over one day: (source, series, dataset)."""
    source = (
        await client.post(
            "/api/sources", json={"type": "synthetic", "name": "plant", "config": {"points": POINTS}}
        )
    ).json()
    points = [{"external_id": p["external_id"], "name": p["external_id"]} for p in POINTS]
    assert (await client.post(f"/api/sources/{source['id']}/series", json=points)).status_code == 200
    series = [
        s["id"] for s in (await client.get("/api/series", params={"source_id": source["id"]})).json()["items"]
    ]
    dataset = await client.post(
        "/api/datasets", json={"name": "plant day", "series_ids": series, "window": WINDOW}
    )
    assert dataset.status_code == 201, dataset.text
    return source["id"], series, dataset.json()["id"]


async def run(client, dataset_id) -> dict:
    r = await client.post("/api/runs", json={"dataset_id": dataset_id, "now": WINDOW["end"]})
    assert r.status_code == 202, r.text
    return (await client.get(f"/api/runs/{r.json()['id']}")).json()


async def test_a_run_fetches_its_gaps_and_checks_them(db_url, fresh_schema, tmp_path):
    fresh_schema("auto")
    app, client = make_client(db_url, tmp_path)
    async with client:
        source_id, series, dataset_id = await prepare(client)
        result = await run(client, dataset_id)
        assert result["status"] == "succeeded", result
        stats = result["stats"]
        assert stats["n_series"] == 2 and stats["n_samples"] == 2 * 1440
        assert stats["fetched"] == {source_id: 2 * 1440} and "fetch_errors" not in stats
        assert stats["missing"] == {}
        # The flatline fault is found in the fetched data.
        findings = (await client.get("/api/findings", params={"series_id": series[0]})).json()["items"]
        assert any(f["check_id"] == "tby.flatline" for f in findings)
        fetches = (await client.get(f"/api/sources/{source_id}/fetches")).json()["items"]
        assert [(f["trigger"], f["run_id"], f["status"]) for f in fetches] == [
            ("run", result["id"], "succeeded")
        ]
        # A second run finds everything cached and fetches nothing.
        again = await run(client, dataset_id)
        assert again["status"] == "succeeded" and "fetched" not in again["stats"]
    await app.state.engine.dispose()


class FailsLater(SyntheticConnector):
    """Answers a first fetch, then refuses."""

    answered = 0

    async def fetch(self, points, start_ns, end_ns):
        if FailsLater.answered >= 1:
            raise ConnectorError("historian offline", retryable=True)
        FailsLater.answered += 1
        async for batch in super().fetch(points, start_ns, end_ns):
            yield batch


@pytest.fixture
def fails_later(monkeypatch):
    FailsLater.answered = 0
    monkeypatch.setitem(connectors._REGISTRY, "synthetic", FailsLater)  # noqa: SLF001


async def test_fetch_errors_do_not_fail_the_run(db_url, fresh_schema, tmp_path, fails_later):
    fresh_schema("auto")
    app, client = make_client(db_url, tmp_path)
    async with client:
        source_id, series, dataset_id = await prepare(client)
        half = {"start": WINDOW["start"], "end": (DAY0 + timedelta(hours=12)).isoformat()}
        assert (await client.post(f"/api/sources/{source_id}/fetches", json=half)).status_code == 202
        result = await run(client, dataset_id)
        assert result["status"] == "succeeded", result
        [error] = result["stats"]["fetch_errors"]
        assert (error["source_id"], error["status"], error["error"]) == (
            source_id,
            "partial",
            "historian offline",
        )
        assert result["stats"]["n_samples"] == 2 * 720
        assert set(result["stats"]["missing"]) == set(series)
    await app.state.engine.dispose()


async def test_budget_zero_turns_fetching_off(db_url, fresh_schema, tmp_path):
    fresh_schema("auto")
    app, client = make_client(db_url, tmp_path, run_fetch_budget_s=0)
    async with client:
        _, _, dataset_id = await prepare(client)
        result = await run(client, dataset_id)
        assert (result["status"], result["error"]) == ("failed", "no cached data in window")
    await app.state.engine.dispose()
