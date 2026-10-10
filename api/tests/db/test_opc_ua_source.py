"""An `opc_ua` source end to end against the in-process asyncua server (spec 023): credentials
with a generated client certificate, the first check naming the server's thumbprint, pinning
it, search, register, a dataset run with findings, and a metadata import."""

from __future__ import annotations

import base64
import os
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from opcua_server import DAY0, MINUTES, USERS, net_policy, running_server
from tabayyun.db.models import AuditEvent
from tabayyun.main import create_app
from tenancy import app_settings, org_session

WINDOW = {"start": DAY0.isoformat(), "end": (DAY0 + timedelta(days=1)).isoformat()}


@pytest.fixture
async def env(db_url, fresh_schema, tmp_path):
    """A client of an app with inline jobs and a master key, the fixture server, and the app."""
    fresh_schema("auto")
    app = create_app(
        app_settings(
            db_url,
            inline_jobs=True,
            cache_url=str(tmp_path),
            master_key=base64.b64encode(os.urandom(32)).decode(),
        )
    )
    app.state.net_policy = net_policy()
    async with running_server() as server:
        client = AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers={"X-Tabayyun-Request": "1"}
        )
        async with client:
            yield client, server, app
    await app.state.engine.dispose()


async def job(client, source_id, path, body):
    r = await client.post(f"/api/sources/{source_id}/{path}", json=body)
    assert r.status_code == 202, r.text
    return (await client.get(f"/api/sources/{source_id}/fetches/{r.json()['id']}")).json()


async def test_set_up_pin_search_fetch_run_and_import(env):
    client, server, app = env
    created = await client.post(
        "/api/sources", json={"type": "opc_ua", "name": "SCADA", "config": {"endpoint_url": server.url}}
    )
    assert created.status_code == 201, created.text
    source_id = created.json()["id"]
    base = f"/api/sources/{source_id}"

    missing = await client.get(f"{base}/client-certificate")
    assert (missing.status_code, missing.json()["detail"]) == (404, "no_client_certificate")
    assert (await client.put(f"{base}/credentials", json={"kind": "anonymous"})).status_code == 204
    cert = (await client.get(f"{base}/client-certificate")).json()
    assert cert["application_uri"] == f"urn:tabayyun:source:{source_id}" and len(cert["sha256"]) == 64
    assert "PRIVATE" not in str(cert)
    [(user, password)] = USERS.items()
    login = {"kind": "username", "username": user, "password": password}
    assert (await client.put(f"{base}/credentials", json=login)).status_code == 204
    assert (await client.get(f"{base}/client-certificate")).json()["sha256"] == cert["sha256"]

    first = await job(client, source_id, "check", None)
    assert first["status"] == "failed" and server.thumbprint in first["error"]
    pinned = {"endpoint_url": server.url, "server_certificate_sha256": server.thumbprint.upper()}
    assert (await client.patch(base, json={"config": pinned})).status_code == 200
    assert (await job(client, source_id, "check", None))["status"] == "succeeded"

    found = await job(client, source_id, "search", {"query": "PV"})
    names = [i["name"] for i in found["result"]["items"]]
    assert names == ["FIC101.PV", "LIC201.PV", "TI102.PV", "FIC201.PV"]
    wanted = [i for i in found["result"]["items"] if i["name"] in ("FIC101.PV", "LIC201.PV")]
    points = [{"external_id": i["external_id"], "name": i["name"]} for i in wanted]
    assert (await client.post(f"{base}/series", json=points)).json()["created"] == 2
    series = {
        s["name"]: s
        for s in (await client.get("/api/series", params={"source_id": source_id})).json()["items"]
    }

    dataset = await client.post(
        "/api/datasets",
        json={"name": "SCADA day", "series_ids": [s["id"] for s in series.values()], "window": WINDOW},
    )
    run = await client.post("/api/runs", json={"dataset_id": dataset.json()["id"], "now": WINDOW["end"]})
    result = (await client.get(f"/api/runs/{run.json()['id']}")).json()
    assert result["status"] == "succeeded", result
    assert result["stats"]["fetched"] == {source_id: 2 * MINUTES} and "fetch_errors" not in result["stats"]
    findings = (await client.get("/api/findings", params={"series_id": series["FIC101.PV"]["id"]})).json()[
        "items"
    ]
    assert any(f["check_id"] == "tby.flatline" for f in findings)

    imported = await job(client, source_id, "metadata", {})
    assert (imported["status"], imported["result"]["updated"]) == ("succeeded", 2), imported
    flow = (await client.get(f"/api/series/{series['FIC101.PV']['id']}")).json()
    assert (flow["unit"], flow["physical_min"], flow["physical_max"]) == ("m³/h", 0.0, 120.0)
    assert (flow["operational_min"], flow["operational_max"], flow["asset_path"]) == (
        10.0,
        90.0,
        "Plant/Area1",
    )
    assert flow["metadata"]["opc_ua"]["data_type"] == "Double"

    async with org_session(app) as session:
        events = (
            await session.scalars(select(AuditEvent).where(AuditEvent.action == "source.credentials_set"))
        ).all()
    assert sorted(e.details["client_certificate"] for e in events) == ["generated", "kept"]
