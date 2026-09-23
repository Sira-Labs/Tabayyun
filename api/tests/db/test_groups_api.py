"""Series groups API (spec 008): validation, conflicts, filters, updates and deletion."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from tabayyun.main import create_app
from tabayyun.settings import Settings

HOUR0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def app(db_url, fresh_schema):
    """App on a fresh schema, executing jobs inline."""
    fresh_schema("auto")
    return create_app(Settings(env="test", database_url=db_url, inline_jobs=True))


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await app.state.engine.dispose()


async def _series(client, name: str) -> str:
    """Upload 24 hourly rows as series `name` and return its id."""
    rows = ["ts,value"] + [f"{(HOUR0 + timedelta(hours=h)).isoformat()},{h % 5}" for h in range(24)]
    r = await client.post(
        "/api/runs",
        files={"file": ("f.csv", ("\n".join(rows) + "\n").encode(), "text/csv")},
        data={"series_id": name},
    )
    assert r.status_code == 202, r.text
    run = (await client.get(f"/api/runs/{r.json()['id']}")).json()
    assert run["status"] == "succeeded", run
    return run["series"][0]["id"]


@pytest.fixture
async def ids(client) -> dict[str, str]:
    return {name: await _series(client, name) for name in ("pt-a", "pt-b", "pt-c")}


def _members(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"series_id": s, "role": r} for s, r in pairs]


async def test_create_read_list_and_filter(client, ids):
    body = {
        "name": "PT-101",
        "kind": "redundant",
        "members": _members((ids["pt-a"], "member"), (ids["pt-b"], "member")),
    }
    r = await client.post("/api/series-groups", json=body)
    assert r.status_code == 201, r.text
    group = r.json()
    assert group["kind"] == "redundant" and group["params"] == {}
    assert [(m["external_id"], m["role"]) for m in group["members"]] == [
        ("pt-a", "member"),
        ("pt-b", "member"),
    ]
    assert (await client.get(f"/api/series-groups/{group['id']}")).json() == group

    balance = {
        "name": "Balance",
        "kind": "balance",
        "members": _members((ids["pt-c"], "input"), (ids["pt-a"], "output")),
        "params": {"loss_max": 0.03},
    }
    assert (await client.post("/api/series-groups", json=balance)).status_code == 201
    listed = (await client.get("/api/series-groups")).json()
    assert [g["name"] for g in listed["items"]] == ["Balance", "PT-101"] and listed["next_cursor"] is None
    only_b = (await client.get("/api/series-groups", params={"series_id": ids["pt-b"]})).json()
    assert [g["name"] for g in only_b["items"]] == ["PT-101"]
    page = (await client.get("/api/series-groups", params={"limit": 1})).json()
    assert len(page["items"]) == 1 and page["next_cursor"]
    rest = (await client.get("/api/series-groups", params={"limit": 1, "cursor": page["next_cursor"]})).json()
    assert [g["name"] for g in rest["items"]] == ["PT-101"]
    assert (await client.get("/api/series-groups", params={"series_id": "nope"})).status_code == 422
    assert (await client.get("/api/series-groups", params={"cursor": "nope"})).status_code == 422


@pytest.mark.parametrize(
    ("kind", "roles", "field", "message"),
    [
        ("related", ["member"], "members", "needs 2 to 32 members, has 1"),
        ("redundant", ["member", "input"], "members", "role `member` only"),
        ("balance", ["input", "member"], "members", "roles `input` and `output` only"),
        ("balance", ["input", "input"], "members", "at least one input and one output"),
    ],
)
async def test_member_rules_422(client, ids, kind, roles, field, message):
    names = ["pt-a", "pt-b", "pt-c"][: len(roles)]
    body = {
        "name": "G",
        "kind": kind,
        "members": _members(*((ids[n], r) for n, r in zip(names, roles, strict=True))),
    }
    r = await client.post("/api/series-groups", json=body)
    assert r.status_code == 422, r.text
    assert r.json()["detail"][0]["loc"] == ["body", field]
    assert message in r.json()["detail"][0]["msg"]


async def test_other_422_paths(client, ids):
    async def post(**body) -> tuple[int, str, str]:
        full = {"name": "G", "kind": "related", **body}
        r = await client.post("/api/series-groups", json=full)
        detail = r.json()["detail"][0]
        return r.status_code, detail["loc"][-1], detail["msg"]

    pair = _members((ids["pt-a"], "member"), (ids["pt-b"], "member"))
    twice = _members((ids["pt-a"], "member"), (ids["pt-a"], "member"))
    assert await post(members=twice) == (422, "members", f"series {ids['pt-a']} is listed twice")
    ghost = str(uuid.uuid4())
    assert await post(members=_members((ids["pt-a"], "member"), (ghost, "member"))) == (
        422,
        "members",
        f"series {ghost} not found",
    )
    many = [{"series_id": str(uuid.uuid4())} for _ in range(33)]
    assert (await post(members=many))[2] == "needs 2 to 32 members, has 33"
    status, field, msg = await post(members=pair, params={"blob": "x" * 9000})
    assert (status, field) == (422, "params") and "8192 bytes" in msg
    # Measured as UTF-8: 3,000 Arabic letters are 6 KB, not the 18 KB of `\uXXXX` escapes.
    r = await client.post(
        "/api/series-groups",
        json={"name": "ar", "kind": "related", "members": pair, "params": {"note": "ت" * 3000}},
    )
    assert r.status_code == 201, r.text
    # Schema-level rules come from the request model.
    assert (await post(members=pair, params=[1]))[:2] == (422, "params")
    assert (await post(members=pair, kind="other"))[:2] == (422, "kind")
    assert (await post(members=pair, name=""))[:2] == (422, "name")
    assert (await post(members=[{"series_id": "not-a-uuid"}, {"series_id": ids["pt-a"]}]))[0] == 422
    assert (await post(members=_members((ids["pt-a"], "boss"), (ids["pt-b"], "member"))))[0] == 422


async def test_name_conflicts_409(client, ids):
    pair = _members((ids["pt-a"], "member"), (ids["pt-b"], "member"))
    assert (
        await client.post("/api/series-groups", json={"name": "G", "kind": "related", "members": pair})
    ).status_code == 201
    other = (
        await client.post("/api/series-groups", json={"name": "H", "kind": "related", "members": pair})
    ).json()
    r = await client.post("/api/series-groups", json={"name": "G", "kind": "redundant", "members": pair})
    assert r.status_code == 409
    assert (await client.patch(f"/api/series-groups/{other['id']}", json={"name": "G"})).status_code == 409
    # Renaming a group to its own name is not a conflict.
    assert (await client.patch(f"/api/series-groups/{other['id']}", json={"name": "H"})).status_code == 200


async def test_patch_and_delete(client, ids):
    pair = _members((ids["pt-a"], "member"), (ids["pt-b"], "member"))
    group = (
        await client.post("/api/series-groups", json={"name": "G", "kind": "redundant", "members": pair})
    ).json()
    url = f"/api/series-groups/{group['id']}"
    three = _members((ids["pt-c"], "member"), (ids["pt-a"], "member"), (ids["pt-b"], "member"))
    r = await client.patch(url, json={"members": three, "params": {"tolerance": 0.5}, "name": "G2"})
    assert r.status_code == 200, r.text
    patched = r.json()
    assert [m["external_id"] for m in patched["members"]] == ["pt-c", "pt-a", "pt-b"]
    assert (
        patched["params"] == {"tolerance": 0.5} and patched["name"] == "G2" and patched["kind"] == "redundant"
    )
    assert patched["updated_at"] >= group["updated_at"]
    # Members are validated against the stored kind; the kind itself cannot change.
    bad = await client.patch(url, json={"members": _members((ids["pt-a"], "input"), (ids["pt-b"], "output"))})
    assert bad.status_code == 422 and bad.json()["detail"][0]["loc"] == ["body", "members"]
    assert (await client.patch(url, json={"kind": "balance"})).status_code == 422
    assert (await client.patch(url, json={"members": None})).status_code == 422
    assert (await client.patch(f"/api/series-groups/{uuid.uuid4()}", json={"name": "X"})).status_code == 404
    assert (await client.delete(url)).status_code == 204
    assert (await client.get(url)).status_code == 404
    assert (await client.delete(url)).status_code == 404
    assert (await client.get("/api/series-groups/not-a-uuid")).status_code == 404
