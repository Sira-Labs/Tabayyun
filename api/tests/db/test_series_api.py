"""Series and sources from uploads, metadata precedence and PATCH (spec 004)."""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from tabayyun.main import create_app
from tenancy import app_settings

HOUR0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def app(db_url, fresh_schema):
    """App on a fresh schema, executing jobs inline in the request's background task."""
    fresh_schema("auto")
    return create_app(app_settings(db_url, inline_jobs=True))


@pytest.fixture
async def client(app):
    """HTTP client whose requests complete their background tasks before returning."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"X-Tabayyun-Request": "1"}
    ) as c:
        yield c
    await app.state.engine.dispose()


def sine_csv(hours: int = 72, *, epoch_seconds: bool = False) -> bytes:
    """Hourly sine between 40 and 60."""
    rows = ["ts,value"]
    for h in range(hours):
        ts = HOUR0 + timedelta(hours=h)
        stamp = str(int(ts.timestamp())) if epoch_seconds else ts.isoformat()
        rows.append(f"{stamp},{50 + 10 * math.sin(h / 24 * 2 * math.pi):.3f}")
    return ("\n".join(rows) + "\n").encode()


async def _upload(client, series_id: str = "demo", csv: bytes | None = None, **form: str) -> dict:
    """Post an upload and return the finished run."""
    r = await client.post(
        "/api/runs",
        files={"file": ("f.csv", csv or sine_csv(), "text/csv")},
        data={"series_id": series_id, **form},
    )
    assert r.status_code == 202, r.text
    run = (await client.get(f"/api/runs/{r.json()['id']}")).json()
    assert run["status"] == "succeeded", run
    return run


async def _series_id(client, run: dict) -> str:
    """Id of the run's series."""
    return run["series"][0]["id"]


async def _physical_limit_max(client, series_id: str) -> float | None:
    """`limit_max` of the open physical-range finding of a series, if any."""
    r = await client.get("/api/findings", params={"series_id": series_id, "check_id": "tby.physical_range"})
    items = r.json()["items"]
    return items[0]["evidence"]["limit_max"] if items else None


async def test_upload_creates_source_and_series_once(client):
    """Two uploads of `demo` make one Uploads source and one series with two runs."""
    first = await _upload(client)
    second = await _upload(client)
    assert await _series_id(client, first) == await _series_id(client, second)
    sources = (await client.get("/api/sources")).json()["items"]
    assert [(s["type"], s["name"], s["n_series"]) for s in sources] == [("upload", "Uploads", 1)]
    [row] = (await client.get("/api/series")).json()["items"]
    assert (row["external_id"], row["name"], row["source_id"]) == ("demo", "demo", sources[0]["id"])
    detail = (await client.get(f"/api/series/{row['id']}")).json()
    assert detail["n_runs"] == 2


async def test_patch_partial_update(client):
    """PATCH changes only the fields it names; null clears a field; unknown ids are 404."""
    series_id = await _series_id(client, await _upload(client, unit="bar"))
    r = await client.patch(
        f"/api/series/{series_id}",
        json={"name": "Pump 1 pressure", "physical_min": 0, "physical_max": 100, "metadata": {"tag": "P1"}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["name"], body["unit"], body["physical_min"], body["physical_max"]) == (
        "Pump 1 pressure",
        "bar",
        0,
        100,
    )
    assert body["metadata"] == {"tag": "P1"}
    before = body["updated_at"]
    cleared = (await client.patch(f"/api/series/{series_id}", json={"physical_max": None})).json()
    assert (
        cleared["physical_max"] is None
        and cleared["physical_min"] == 0
        and cleared["name"] == "Pump 1 pressure"
    )
    assert cleared["updated_at"] >= before
    assert (await client.patch(f"/api/series/{uuid.uuid4()}", json={"unit": "bar"})).status_code == 404
    assert (await client.patch("/api/series/nope", json={"unit": "bar"})).status_code == 404


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"physical_min": 10, "physical_max": 5}, "physical_max"),
        ({"physical_max": -1}, "physical_max"),
        ({"operational_min": 5, "operational_max": 1}, "operational_max"),
        ({"operational_max": 150}, "operational_max"),
        ({"operational_min": -5}, "operational_min"),
        ({"resolution": 0}, "resolution"),
        ({"expected_interval_ns": -1}, "expected_interval_ns"),
        ({"kind": "weird"}, "kind"),
        ({"unit": "x" * 33}, "unit"),
        ({"metadata": {"blob": "x" * 9000}}, "metadata"),
        ({"bogus": 1}, "bogus"),
        ({"name": None}, "name"),
        ({"physical_min": "NaN"}, "physical_min"),
    ],
)
async def test_patch_partial_update_and_validation(client, body, field):
    """Invalid values and combinations (checked on the merged metadata) are 422 naming the field."""
    series_id = await _series_id(client, await _upload(client))
    ok = await client.patch(f"/api/series/{series_id}", json={"physical_min": 0, "physical_max": 100})
    assert ok.status_code == 200
    r = await client.patch(f"/api/series/{series_id}", json=body)
    assert r.status_code == 422, r.text
    locs = [e["loc"] for e in r.json()["detail"]]
    assert any(field in loc for loc in locs), locs
    unchanged = (await client.get(f"/api/series/{series_id}")).json()
    assert (unchanged["physical_min"], unchanged["physical_max"]) == (0, 100)


async def test_stored_metadata_used_on_next_run(client):
    """A stored physical_max applies to the next upload that does not pass one."""
    series_id = await _series_id(client, await _upload(client))
    assert await _physical_limit_max(client, series_id) is None
    r = await client.patch(f"/api/series/{series_id}", json={"physical_max": 55})
    assert r.status_code == 200
    await _upload(client)
    assert await _physical_limit_max(client, series_id) == 55


async def test_form_overrides_and_persists(client):
    """A limit passed with the upload is used for that run and saved on the series."""
    series_id = await _series_id(client, await _upload(client))
    await client.patch(f"/api/series/{series_id}", json={"physical_min": 30, "physical_max": 55})
    await _upload(client, physical_max="58")
    assert await _physical_limit_max(client, series_id) == 58
    stored = (await client.get(f"/api/series/{series_id}")).json()
    assert (stored["physical_min"], stored["physical_max"]) == (30, 58)
    # A form limit that contradicts the stored series is refused before a run exists.
    r = await client.post(
        "/api/runs",
        files={"file": ("f.csv", sine_csv(), "text/csv")},
        data={"series_id": "demo", "physical_max": "20"},
    )
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "physical_max"]


async def test_patch_during_a_run_fails_the_run_instead_of_saving_bad_limits(client, app, monkeypatch):
    """A PATCH that lands while the core runs is re-checked on the locked row: the run fails."""
    import asyncio

    from tabayyun.services import runs as runs_service

    series_id = await _series_id(client, await _upload(client))
    real_run_checks = runs_service.core.run_checks
    loop = asyncio.get_running_loop()

    def run_checks_while_patched(*args, **kwargs):
        report = real_run_checks(*args, **kwargs)

        async def patch() -> None:
            r = await client.patch(f"/api/series/{series_id}", json={"physical_min": 60})
            assert r.status_code == 200, r.text

        asyncio.run_coroutine_threadsafe(patch(), loop).result()
        return report

    monkeypatch.setattr(runs_service.core, "run_checks", run_checks_while_patched)
    r = await client.post(
        "/api/runs",
        files={"file": ("f.csv", sine_csv(), "text/csv")},
        data={"series_id": "demo", "physical_max": "58"},
    )
    assert r.status_code == 202, r.text
    run = (await client.get(f"/api/runs/{r.json()['id']}")).json()
    assert run["status"] == "failed"
    assert run["error"].startswith("invalid series metadata: physical_max")
    stored = (await client.get(f"/api/series/{series_id}")).json()
    assert (stored["physical_min"], stored["physical_max"], stored["n_runs"]) == (60, None, 1)


async def test_series_summary_has_latest_score_and_open_findings(client):
    """Summary and detail carry the newest score, unresolved findings and the last run time."""
    first = await _upload(client, physical_max="55")
    second = await _upload(client, physical_max="55")
    series_id = await _series_id(client, second)
    findings = (await client.get("/api/findings", params={"series_id": series_id})).json()["items"]
    assert findings
    [row] = (await client.get("/api/series")).json()["items"]
    assert row["latest_score"]["overall"] == second["series"][0]["score"]
    assert row["open_findings"] == len(findings)
    assert row["last_run_at"] is not None
    await client.patch(f"/api/findings/{findings[0]['id']}", json={"status": "resolved", "reason": "fixed"})
    detail = (await client.get(f"/api/series/{series_id}")).json()
    assert detail["open_findings"] == len(findings) - 1
    assert detail["n_runs"] == 2 and first["id"] != second["id"]


async def test_series_search_and_pagination(client):
    """`q` matches name or external id case-insensitively; pages are stable and disjoint."""
    for external_id in ("pump-1", "pump-2", "valve-a", "meter_%"):
        await _upload(client, external_id, csv=sine_csv(30))
    valve = next(
        s for s in (await client.get("/api/series")).json()["items"] if s["external_id"] == "valve-a"
    )
    await client.patch(f"/api/series/{valve['id']}", json={"name": "Main PUMP valve"})
    hits = (await client.get("/api/series", params={"q": "pump"})).json()["items"]
    assert sorted(s["external_id"] for s in hits) == ["pump-1", "pump-2", "valve-a"]
    literal = (await client.get("/api/series", params={"q": "_%"})).json()["items"]
    assert [s["external_id"] for s in literal] == ["meter_%"]
    pages, cursor = [], None
    while True:
        params = {"limit": 1, **({"cursor": cursor} if cursor else {})}
        body = (await client.get("/api/series", params=params)).json()
        pages.extend(s["name"] for s in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert pages == sorted(pages) and len(pages) == 4
    assert len((await client.get("/api/series", params={"kind": "measurement"})).json()["items"]) == 4
    assert (await client.get("/api/series", params={"kind": "other"})).status_code == 422
    assert (await client.get("/api/series", params={"cursor": "garbage"})).status_code == 422


async def test_epoch_seconds_upload_records_the_unit(client):
    """Epoch seconds give the same window as RFC 3339 text and the run records the unit."""
    text_run = await _upload(client, "a", csv=sine_csv())
    epoch_run = await _upload(client, "b", csv=sine_csv(epoch_seconds=True))
    assert epoch_run["window"] == text_run["window"]
    assert (text_run["stats"]["ts_unit"], epoch_run["stats"]["ts_unit"]) == ("text", "s")
    r = await client.post(
        "/api/runs",
        files={"file": ("f.csv", sine_csv(epoch_seconds=True), "text/csv")},
        data={"series_id": "c", "ts_unit": "ns"},
    )
    assert r.status_code == 422 and "1970" in r.json()["detail"]
