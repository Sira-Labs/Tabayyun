"""A `pi_web_api` source end to end against the fake PI Web API (spec 022): create it with
credentials, search, register, fetch, run a dataset with findings, import metadata (and again,
without and with `overwrite`), and a fetch that meets a deleted point."""

from __future__ import annotations

import base64
import os
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from pi_fake import CONFIG, CREDENTIALS, FLOW, FLOW_ATTRIBUTE, LEVEL, FakePiWebApi, net_policy
from tabayyun.main import create_app
from tenancy import app_settings

DAY0 = datetime(2026, 9, 1, tzinfo=UTC)
WINDOW = {"start": DAY0.isoformat(), "end": (DAY0 + timedelta(days=1)).isoformat()}


@pytest.fixture
async def pi(db_url, fresh_schema, tmp_path):
    """A client of an app with inline jobs, a master key and the fake PI Web API behind its policy."""
    fresh_schema("auto")
    fake = FakePiWebApi()
    app = create_app(
        app_settings(
            db_url,
            inline_jobs=True,
            cache_url=str(tmp_path),
            master_key=base64.b64encode(os.urandom(32)).decode(),
        )
    )
    app.state.net_policy = net_policy(fake)
    client = AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"X-Tabayyun-Request": "1"}
    )
    async with client:
        yield client, fake
    await app.state.engine.dispose()


async def job(client, source_id, path, body):
    r = await client.post(f"/api/sources/{source_id}/{path}", json=body)
    assert r.status_code == 202, r.text
    return (await client.get(f"/api/sources/{source_id}/fetches/{r.json()['id']}")).json()


async def setup_source(client) -> str:
    config = {**CONFIG, "asset_database": "\\\\AFSRV01\\Plant"}
    source = await client.post("/api/sources", json={"type": "pi_web_api", "name": "PI", "config": config})
    assert source.status_code == 201, source.text
    source_id = source.json()["id"]
    assert (await client.put(f"/api/sources/{source_id}/credentials", json=CREDENTIALS)).status_code == 204
    return source_id


async def series_by_external_id(client, source_id) -> dict[str, dict]:
    items = (await client.get("/api/series", params={"source_id": source_id})).json()["items"]
    return {s["external_id"]: s for s in items}


async def test_search_register_fetch_run_and_import(pi):
    client, fake = pi
    source_id = await setup_source(client)
    check = await job(client, source_id, "check", None)
    assert check["status"] == "succeeded", check

    found = await job(client, source_id, "search", {"query": "FIC101"})
    assert found["status"] == "succeeded", found
    assert [i["external_id"] for i in found["result"]["items"]] == [FLOW]
    # AF attributes match by their own name; "Flow design" has no PI Point reference.
    found = await job(client, source_id, "search", {"query": "flow"})
    assert [i["external_id"] for i in found["result"]["items"]] == [FLOW_ATTRIBUTE]
    points = [{"external_id": FLOW, "name": "Inlet flow"}, {"external_id": LEVEL, "name": "Tank level"}]
    assert (await client.post(f"/api/sources/{source_id}/series", json=points)).json()["created"] == 2
    series = await series_by_external_id(client, source_id)

    dataset = await client.post(
        "/api/datasets",
        json={"name": "PI day", "series_ids": [s["id"] for s in series.values()], "window": WINDOW},
    )
    assert dataset.status_code == 201, dataset.text
    run = await client.post("/api/runs", json={"dataset_id": dataset.json()["id"], "now": WINDOW["end"]})
    result = (await client.get(f"/api/runs/{run.json()['id']}")).json()
    assert result["status"] == "succeeded", result
    # The connector reads 1441 flow values; the cache keeps one per timestamp (spec 006).
    assert result["stats"]["fetched"] == {source_id: 1440 + 1440}
    assert "fetch_errors" not in result["stats"]
    findings = (await client.get("/api/findings", params={"series_id": series[FLOW]["id"]})).json()["items"]
    assert any(f["check_id"] == "tby.flatline" for f in findings)
    # Each point was read through PI Web API's batch lookup and its recorded values.
    assert {r.url.path for r in fake.requests} >= {
        "/piwebapi/batch",
        "/piwebapi/streams/F1DPFIC101PV/recorded",
        "/piwebapi/streams/F1DPLIC201PV/recorded",
    }
    assert all(r.headers["x-requested-with"] == "tabayyun" for r in fake.requests)

    imported = await job(client, source_id, "metadata", {})
    assert imported["status"] == "succeeded", imported
    assert (imported["result"]["updated"], imported["result"]["failed"]) == (2, 0)
    flow = (await client.get(f"/api/series/{series[FLOW]['id']}")).json()
    assert (flow["unit"], flow["physical_min"], flow["physical_max"]) == ("m3/h", 0.0, 200.0)
    assert flow["metadata"]["pi_web_api"]["compdev"] == 0.2
    assert flow["metadata"]["pi_web_api"]["description"] == "Inlet flow"


async def test_metadata_keeps_edits_unless_overwritten_and_reads_af_limits(pi):
    client, _ = pi
    source_id = await setup_source(client)
    points = [{"external_id": FLOW_ATTRIBUTE, "name": "Flow (AF)"}]
    await client.post(f"/api/sources/{source_id}/series", json=points)
    [attr] = (await series_by_external_id(client, source_id)).values()
    patched = await client.patch(f"/api/series/{attr['id']}", json={"unit": "t/h", "physical_max": 500})
    assert patched.status_code == 200, patched.text

    kept = await job(client, source_id, "metadata", {"series_ids": [attr["id"]]})
    assert kept["result"]["series"][attr["id"]]["changed"] == [
        "physical_min",
        "operational_min",
        "operational_max",
        "asset_path",
        "metadata",
    ]
    detail = (await client.get(f"/api/series/{attr['id']}")).json()
    assert (detail["unit"], detail["physical_min"], detail["physical_max"]) == ("t/h", 0.0, 500.0)
    assert (detail["operational_min"], detail["operational_max"]) == (20.0, 90.0)
    assert detail["asset_path"] == "\\\\AFSRV01\\Plant\\Area1\\FIC101"

    replaced = await job(client, source_id, "metadata", {"overwrite": True})
    assert replaced["result"]["series"][attr["id"]]["changed"] == ["unit", "physical_max"]
    detail = (await client.get(f"/api/series/{attr['id']}")).json()
    assert (detail["unit"], detail["physical_max"]) == ("m3/h", 120.0)


async def test_a_deleted_point_leaves_the_others_fetched(pi):
    client, fake = pi
    source_id = await setup_source(client)
    points = [{"external_id": FLOW, "name": "flow"}, {"external_id": LEVEL, "name": "level"}]
    await client.post(f"/api/sources/{source_id}/series", json=points)
    series = await series_by_external_id(client, source_id)
    fake.deleted.add("F1DPLIC201PV")
    fetch = await job(client, source_id, "fetches", WINDOW)
    assert (fetch["status"], fetch["rows"]) == ("partial", 1440)
    assert fetch["error"].startswith("1 point(s) failed:")
    assert list(fetch["result"]["point_errors"]) == [series[LEVEL]["id"]]
    detail = (await client.get(f"/api/sources/{source_id}")).json()
    assert detail["health"]["status"] == "degraded"


async def test_refused_credentials_land_in_health(pi):
    client, fake = pi
    source_id = await setup_source(client)
    fake.fail_status = 401
    check = await job(client, source_id, "check", None)
    assert (check["status"], check["error"]) == ("failed", "PI Web API refused the credentials")
    health = (await client.get(f"/api/sources/{source_id}")).json()["health"]
    assert health["last_error"]["retryable"] is False
