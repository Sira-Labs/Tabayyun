"""The sources API (specs 021, 022): create, validate, patch, write-only credentials, series,
checks, fetches, point searches and metadata imports, roles, 404 across workspaces, audit
events, and inline jobs end to end."""

from __future__ import annotations

import base64
import json
import os
import uuid

import pytest
from pydantic import BaseModel
from structlog.testing import capture_logs

from admin_support import ALICE, BOB, MIA, NED, ORG_B
from tabayyun import connectors
from tabayyun.connectors import Connector
from tabayyun.connectors.synthetic import SyntheticConnector
from tabayyun.db.models import DEFAULT_ORG_ID, DEFAULT_WORKSPACE_ID

POINTS = [{"external_id": "flow", "base": 50, "amplitude": 10}]
DAY = {"start": "2026-09-01T00:00:00Z", "end": "2026-09-02T00:00:00Z"}
SECRET = "pi-password-do-not-echo"


class Creds(BaseModel):
    username: str
    password: str


class WithCredentials(SyntheticConnector):
    """The synthetic connector taking credentials, to exercise the credentials routes."""

    credentials_model = Creds


@pytest.fixture
def with_credentials(monkeypatch):
    monkeypatch.setitem(connectors._REGISTRY, "synthetic", WithCredentials)  # noqa: SLF001


def master_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


async def create(client, name="plant-a", **config):
    return await client.post(
        "/api/sources", json={"type": "synthetic", "name": name, "config": {"points": POINTS, **config}}
    )


async def test_create_get_list_and_audit(admin_env):
    async with admin_env.client(ALICE) as alice:
        r = await create(alice, poll_interval_s=300)
        assert r.status_code == 201, r.text
        body = r.json()
        assert (body["type"], body["enabled"], body["connector"], body["n_series"]) == (
            "synthetic",
            True,
            True,
            0,
        )
        assert body["health"] == {"status": "unknown"} and body["credentials"] == {
            "set": False,
            "updated_at": None,
        }
        assert (
            body["config"]["poll_interval_s"] == 300 and body["config"]["points"][0]["external_id"] == "flow"
        )
        listed = (await alice.get("/api/sources")).json()["items"]
        assert [(s["name"], s["connector"], s["health_status"]) for s in listed] == [
            ("plant-a", True, "unknown")
        ]
        assert (await alice.get(f"/api/sources/{body['id']}")).json()["id"] == body["id"]
        dup = await create(alice)
        assert (dup.status_code, dup.json()["detail"]) == (409, "name_taken")
    [event] = admin_env.events("source.created")
    assert event.details == {"type": "synthetic", "name": "plant-a"}
    assert event.workspace_id == DEFAULT_WORKSPACE_ID


@pytest.mark.parametrize(
    ("body", "detail", "loc"),
    [
        ({"type": "upload", "name": "x"}, "not_a_connector", None),
        (
            {"type": "synthetic", "name": "x", "config": {"max_points": 5000}},
            "invalid_config",
            ["max_points"],
        ),
        ({"type": "synthetic", "name": "x", "config": {"surprise": 1}}, "invalid_config", ["surprise"]),
    ],
)
async def test_create_refusals(admin_env, body, detail, loc):
    async with admin_env.client(ALICE) as alice:
        r = await alice.post("/api/sources", json=body)
    assert (r.status_code, r.json()["detail"]) == (422, detail)
    if loc:
        assert [e["loc"] for e in r.json()["errors"]] == [loc] and "input" not in r.json()["errors"][0]


async def test_patch_and_disable(admin_env):
    async with admin_env.client(ALICE) as alice:
        source = (await create(alice)).json()
        r = await alice.patch(f"/api/sources/{source['id']}", json={"name": "plant-b", "enabled": False})
        assert (r.status_code, r.json()["name"], r.json()["enabled"]) == (200, "plant-b", False)
        bad = await alice.patch(f"/api/sources/{source['id']}", json={"config": {"interval_s": 0}})
        assert (bad.status_code, bad.json()["detail"]) == (422, "invalid_config")
        fetch = await alice.post(f"/api/sources/{source['id']}/fetches", json=DAY)
        assert (fetch.status_code, fetch.json()["detail"]) == (409, "source_disabled")
    [event] = admin_env.events("source.updated")
    assert event.details == {"changed": ["name", "enabled"], "enabled": False}


async def test_upload_sources_are_not_connectors(admin_env):
    upload = uuid.uuid4()
    admin_env.sql(
        "INSERT INTO sources (id, org_id, workspace_id, type, name) VALUES (:i, :o, :w, 'upload', 'Uploads')",
        i=upload,
        o=DEFAULT_ORG_ID,
        w=DEFAULT_WORKSPACE_ID,
    )
    async with admin_env.client(ALICE) as alice:
        for method, path, body in [
            ("PATCH", "", {"name": "x"}),
            ("PUT", "/credentials", {"a": 1}),
            ("POST", "/series", []),
            ("POST", "/check", None),
            ("POST", "/fetches", DAY),
            ("POST", "/search", {"query": "x"}),
            ("POST", "/metadata", {}),
        ]:
            r = await alice.request(method, f"/api/sources/{upload}{path}", json=body)
            assert (r.status_code, r.json()["detail"]) == (409, "not_a_connector"), path
        detail = (await alice.get(f"/api/sources/{upload}")).json()
        assert (detail["connector"], detail["type"]) == (False, "upload")


# Credentials


async def test_credentials_are_write_only_and_encrypted(admin_env, with_credentials):
    async with admin_env.client(ALICE, master_key=master_key()) as alice:
        source = (await create(alice)).json()
        with capture_logs() as logs:
            r = await alice.put(
                f"/api/sources/{source['id']}/credentials", json={"username": "svc", "password": SECRET}
            )
        assert r.status_code == 204
        detail = await alice.get(f"/api/sources/{source['id']}")
        assert detail.json()["credentials"]["set"] is True and SECRET not in detail.text
        bad = await alice.put(f"/api/sources/{source['id']}/credentials", json={"username": SECRET})
        assert (
            bad.status_code == 422
            and bad.json()["detail"] == "invalid_credentials"
            and SECRET not in bad.text
        )
        assert (await alice.delete(f"/api/sources/{source['id']}/credentials")).status_code == 204
        assert (await alice.get(f"/api/sources/{source['id']}")).json()["credentials"]["set"] is False
    assert SECRET not in json.dumps(logs, default=str)
    assert admin_env.sql("SELECT ciphertext FROM source_credentials") == []  # cleared
    events = admin_env.events()
    assert [e.action for e in events if e.action.startswith("source.credentials")] == [
        "source.credentials_set",
        "source.credentials_cleared",
    ]
    assert all(SECRET not in json.dumps(e.details) for e in events)


async def test_ciphertext_holds_no_plaintext(admin_env, with_credentials):
    async with admin_env.client(ALICE, master_key=master_key()) as alice:
        source = (await create(alice)).json()
        await alice.put(
            f"/api/sources/{source['id']}/credentials", json={"username": "svc", "password": SECRET}
        )
    [(ciphertext, key_id)] = admin_env.sql("SELECT ciphertext, key_id FROM source_credentials")
    assert SECRET.encode() not in bytes(ciphertext) and len(key_id) == 8


async def test_credentials_need_a_master_key_and_a_connector_that_takes_them(admin_env, with_credentials):
    async with admin_env.client(ALICE) as alice:
        source = (await create(alice)).json()
        r = await alice.put(
            f"/api/sources/{source['id']}/credentials", json={"username": "a", "password": "b"}
        )
        assert (r.status_code, r.json()["detail"]) == (503, "credentials_unavailable")


async def test_synthetic_takes_no_credentials(admin_env):
    async with admin_env.client(ALICE, master_key=master_key()) as alice:
        source = (await create(alice)).json()
        r = await alice.put(f"/api/sources/{source['id']}/credentials", json={"token": "t"})
    assert (r.status_code, r.json()["detail"]) == (422, "no_credentials")


# Series and fetches


async def test_register_series_fetch_inline_and_history(admin_env, tmp_path):
    async with admin_env.client(ALICE, cache_url=str(tmp_path)) as alice:
        source = (await create(alice)).json()
        points = [
            {"external_id": "flow", "name": "Flow", "unit": "m3/h"},
            {"external_id": "gone", "name": "Gone"},
        ]
        r = await alice.post(f"/api/sources/{source['id']}/series", json=points)
        assert r.json() == {"created": 2, "existing": 0}
        assert (await alice.post(f"/api/sources/{source['id']}/series", json=points)).json() == {
            "created": 0,
            "existing": 2,
        }
        queued = await alice.post(f"/api/sources/{source['id']}/fetches", json=DAY)
        assert queued.status_code == 202
        history = (await alice.get(f"/api/sources/{source['id']}/fetches")).json()["items"]
        # "gone" is not a configured point: the fetch keeps flow's day and ends partial (spec 022).
        assert [(f["id"], f["status"], f["trigger"], f["rows"]) for f in history] == [
            (queued.json()["id"], "partial", "manual", 1440)
        ]
        assert history[0]["error"] == "1 point(s) failed: point not configured: gone"
        detail = (await alice.get(f"/api/sources/{source['id']}")).json()
        assert detail["health"]["status"] == "degraded" and detail["n_series"] == 2
        check = await alice.post(f"/api/sources/{source['id']}/check")
        assert check.status_code == 202
        statuses = [
            f["trigger"] for f in (await alice.get(f"/api/sources/{source['id']}/fetches")).json()["items"]
        ]
        assert statuses == ["check", "manual"]
    assert [e.details for e in admin_env.events("source.series_registered")] == [
        {"created": 2, "existing": 0},
        {"created": 0, "existing": 2},
    ]
    [event] = [e for e in admin_env.events("source.fetch_requested") if e.details["trigger"] == "manual"]
    assert event.details["series"] is None and event.details["force"] is False


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"start": DAY["end"], "end": DAY["start"]}, "invalid_window"),
        ({"start": "2024-01-01T00:00:00Z", "end": "2026-01-01T00:00:00Z"}, "invalid_window"),
        ({**DAY, "series_ids": [str(uuid.uuid4())]}, "unknown_series"),
    ],
)
async def test_fetch_refusals(admin_env, body, detail):
    async with admin_env.client(ALICE) as alice:
        source = (await create(alice)).json()
        r = await alice.post(f"/api/sources/{source['id']}/fetches", json=body)
    assert (r.status_code, r.json()["detail"]) == (422, detail)


async def test_queued_fetch_defers_a_locked_job(admin_env):
    async with admin_env.client(ALICE, inline_jobs=False) as alice:
        source = (await create(alice)).json()
        fetch = (await alice.post(f"/api/sources/{source['id']}/fetches", json=DAY)).json()
    [(task, lock, args)] = admin_env.sql("SELECT task_name, lock, args FROM procrastinate_jobs")
    assert (task, lock) == ("tabayyun.fetch_window", f"source:{source['id']}")
    assert args == {"fetch_id": fetch["id"], "org_id": str(DEFAULT_ORG_ID)}


# Roles and tenants


async def test_roles(admin_env):
    admin_env.sql(
        "INSERT INTO workspace_memberships (org_id, workspace_id, user_id, role) "
        "VALUES (:o, :w, :u, 'editor')",
        o=DEFAULT_ORG_ID,
        w=DEFAULT_WORKSPACE_ID,
        u=NED,
    )
    async with (
        admin_env.client(ALICE) as alice,
        admin_env.client(NED) as editor,
        admin_env.client(MIA) as viewer,
    ):
        source = (await create(alice)).json()
        base = f"/api/sources/{source['id']}"
        assert (await viewer.get(base)).status_code == 200
        assert (await viewer.get(f"{base}/fetches")).status_code == 200
        for client, method, path, body, status in [
            (viewer, "POST", "/series", [], 403),
            (viewer, "POST", "/fetches", DAY, 403),
            (viewer, "POST", "/check", None, 403),
            (viewer, "POST", "/search", {"query": "x"}, 403),
            (viewer, "POST", "/metadata", {}, 403),
            (editor, "POST", "/series", [], 200),
            (editor, "POST", "/check", None, 202),
            (editor, "POST", "/search", {"query": "x"}, 202),
            (editor, "POST", "/metadata", {}, 202),
            (editor, "PATCH", "", {"name": "x"}, 403),
            (editor, "PUT", "/credentials", {}, 403),
            (editor, "DELETE", "/credentials", None, 403),
        ]:
            r = await client.request(method, base + path, json=body)
            assert r.status_code == status, (method, path, r.text)
        assert (await editor.post("/api/sources", json={"type": "synthetic", "name": "e"})).status_code == 403


async def test_another_workspace_sees_404(admin_env):
    async with admin_env.client(ALICE) as alice, admin_env.client(BOB, org=ORG_B) as bob:
        source = (await create(alice)).json()
        base = f"/api/sources/{source['id']}"
        check = (await alice.post(f"{base}/check")).json()["id"]
        for method, path, body in [
            ("POST", "/search", {"query": "x"}),
            ("POST", "/metadata", {}),
            ("GET", f"/fetches/{check}", None),
            ("GET", "", None),
            ("PATCH", "", {"name": "x"}),
            ("PUT", "/credentials", {}),
            ("POST", "/series", []),
            ("POST", "/check", None),
            ("POST", "/fetches", DAY),
            ("GET", "/fetches", None),
        ]:
            r = await bob.request(method, base + path, json=body)
            assert r.status_code == 404, (method, path)
        assert (await bob.get("/api/sources")).json()["items"] == []


async def test_rotation_re_encrypts_under_the_new_key(admin_env, db_url, with_credentials):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from tabayyun import secrets
    from tabayyun.db.models import SourceCredentials

    old, new = os.urandom(32), os.urandom(32)
    async with admin_env.client(ALICE, master_key=base64.b64encode(old).decode()) as alice:
        source = (await create(alice)).json()
        await alice.put(
            f"/api/sources/{source['id']}/credentials", json={"username": "svc", "password": SECRET}
        )
    keyring = secrets.Keyring(new, previous=old)
    engine = create_async_engine(db_url)  # the owner, as `python -m tabayyun.secrets rotate` runs
    try:
        async with async_sessionmaker(engine)() as session, session.begin():
            assert await secrets.rotate(session, keyring) == 1
        async with async_sessionmaker(engine)() as session, session.begin():
            assert await secrets.rotate(session, keyring) == 0
            row = await session.get(SourceCredentials, uuid.UUID(source["id"]))
            assert row.key_id == secrets.key_id_of(new)
            loaded = await secrets.load(
                session, secrets.Keyring(new), org_id=DEFAULT_ORG_ID, source_id=uuid.UUID(source["id"])
            )
            assert loaded == {"username": "svc", "password": SECRET}
    finally:
        await engine.dispose()


# Point search, metadata import and one job (spec 022)


async def test_search_and_metadata_jobs_inline(admin_env, tmp_path):
    points = [{"external_id": "flow", "name": "Inlet flow", "unit": "m3/h", "base": 50}]
    async with admin_env.client(ALICE, cache_url=str(tmp_path)) as alice:
        source = (await create(alice, points=points)).json()
        base = f"/api/sources/{source['id']}"
        search = await alice.post(f"{base}/search", json={"query": " flo ", "limit": 5})
        assert search.status_code == 202, search.text
        job = (await alice.get(f"{base}/fetches/{search.json()['id']}")).json()
        assert (job["trigger"], job["status"], job["params"]) == (
            "search",
            "succeeded",
            {"query": "flo", "limit": 5},
        )
        assert job["result"] == {
            "items": [{"external_id": "flow", "name": "Inlet flow", "unit": "m3/h", "description": None}],
            "truncated": False,
        }
        await alice.post(
            f"{base}/series",
            json=[{"external_id": i["external_id"], "name": "flow"} for i in job["result"]["items"]],
        )
        unknown = await alice.post(f"{base}/metadata", json={"series_ids": [str(uuid.uuid4())]})
        assert (unknown.status_code, unknown.json()["detail"]) == (422, "unknown_series")
        imported = await alice.post(f"{base}/metadata", json={})
        job = (await alice.get(f"{base}/fetches/{imported.json()['id']}")).json()
        assert (job["trigger"], job["status"], job["params"]) == (
            "metadata",
            "succeeded",
            {"overwrite": False},
        )
        assert (job["result"]["updated"], job["result"]["failed"]) == (1, 0)
        [series] = (await alice.get("/api/series", params={"source_id": source["id"]})).json()["items"]
        assert series["unit"] == "m3/h"
        listed = (await alice.get(f"{base}/fetches")).json()["items"]
        assert [f["trigger"] for f in listed] == ["metadata", "search"] and "result" not in listed[0]
        other = (await create(alice, name="plant-b")).json()
        missing = await alice.get(f"/api/sources/{other['id']}/fetches/{search.json()['id']}")
        assert missing.status_code == 404
    requested = [e.details for e in admin_env.events("source.fetch_requested")]
    assert {"trigger": "search", "query": "flo", "limit": 5, "series": None} == {
        k: v for k, v in requested[0].items() if k != "fetch_id"
    }
    [imported_event] = admin_env.events("source.metadata_imported")
    assert (imported_event.actor_user_id, imported_event.details["updated"]) == (ALICE, 1)


class Bare(SyntheticConnector):
    """A connector without search or describe."""

    search = Connector.search  # type: ignore[assignment]
    describe = Connector.describe  # type: ignore[assignment]


async def test_search_and_metadata_need_a_connector_that_has_them(admin_env, monkeypatch):
    monkeypatch.setitem(connectors._REGISTRY, "synthetic", Bare)  # noqa: SLF001
    async with admin_env.client(ALICE) as alice:
        source = (await create(alice)).json()
        for path, body in [("/search", {"query": "x"}), ("/metadata", {})]:
            r = await alice.post(f"/api/sources/{source['id']}{path}", json=body)
            assert (r.status_code, r.json()["detail"]) == (409, "not_supported"), path


@pytest.mark.parametrize(
    "body",
    [
        {"query": ""},
        {"query": "   "},
        {"query": "x" * 201},
        {"query": "x", "limit": 0},
        {"query": "x", "limit": 1001},
    ],
)
async def test_search_validation(admin_env, body):
    async with admin_env.client(ALICE) as alice:
        source = (await create(alice)).json()
        assert (await alice.post(f"/api/sources/{source['id']}/search", json=body)).status_code == 422
