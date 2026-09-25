"""Datasets API (spec 008): validation of series and windows, reads, updates and deletion."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from tabayyun.main import create_app
from tabayyun.services.datasets import DatasetError, parse_last, resolve_window, window_policy
from tenancy import app_settings

HOUR0 = datetime(2026, 1, 1, tzinfo=UTC)
WEEK = {"start": "2026-01-01T00:00:00Z", "end": "2026-01-08T00:00:00Z"}


@pytest.fixture
def app(db_url, fresh_schema):
    fresh_schema("auto")
    return create_app(app_settings(db_url, inline_jobs=True))


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await app.state.engine.dispose()


async def _series(client, name: str) -> str:
    rows = ["ts,value"] + [f"{(HOUR0 + timedelta(hours=h)).isoformat()},{h % 5}" for h in range(24)]
    r = await client.post(
        "/api/runs",
        files={"file": ("f.csv", ("\n".join(rows) + "\n").encode(), "text/csv")},
        data={"series_id": name},
    )
    run = (await client.get(f"/api/runs/{r.json()['id']}")).json()
    assert run["status"] == "succeeded", run
    return run["series"][0]["id"]


@pytest.fixture
async def ids(client) -> dict[str, str]:
    return {name: await _series(client, name) for name in ("b-meter", "a-meter")}


def test_window_rules():
    assert window_policy({"last": " 7d "}) == {"last": "7d"}
    assert window_policy(WEEK) == {"start": "2026-01-01T00:00:00+00:00", "end": "2026-01-08T00:00:00+00:00"}
    assert parse_last("90m") == timedelta(minutes=90) and parse_last("2w") == timedelta(days=14)
    for bad, message in [
        ({"last": "59m"}, "between 1h and 400d"),
        ({"last": "401d"}, "between 1h and 400d"),
        ({"last": "7x"}, "must look like"),
        ({"start": WEEK["end"], "end": WEEK["start"]}, "start must be before end"),
        ({"start": WEEK["start"], "end": WEEK["start"]}, "start must be before end"),
        ({"start": "yesterday", "end": WEEK["end"]}, "not RFC 3339"),
        ({"start": WEEK["start"]}, "either start and end, or last"),
        ({"last": "7d", **WEEK}, "either start and end, or last"),
        ({}, "either start and end, or last"),
        # Wholly outside the core's `i64` ns (1677-09-21 to 2262-04-11): nothing could be read.
        ({"start": "2300-01-01T00:00:00Z", "end": "2301-01-01T00:00:00Z"}, "outside the supported range"),
        ({"start": "1500-01-01T00:00:00Z", "end": "1600-01-01T00:00:00Z"}, "outside the supported range"),
        # Judged on the requested nanosecond: 1 ns past i64::MAX, which microseconds would hide.
        (
            {"start": "2262-04-11T23:47:16.854775808Z", "end": "2300-01-01T00:00:00Z"},
            "outside the supported range",
        ),
        ({"start": "9223372036854775808", "end": "2300-01-01T00:00:00Z"}, "outside the supported range"),
        (
            {"start": "2262-04-11T23:47:16,854775808Z", "end": "2300-01-01T00:00:00Z"},
            "outside the supported range",
        ),
        # Inside as requested, but stored to the microsecond the end falls below the range.
        ({"start": "1600-01-01T00:00:00Z", "end": str(-(2**63) + 1)}, "outside the supported range"),
    ]:
        with pytest.raises(DatasetError, match=message):
            window_policy(bad)
    # A window reaching past the range keeps the part inside it, down to i64::MAX itself.
    assert window_policy({"start": "2262-04-11T00:00:00Z", "end": "2300-01-01T00:00:00Z"})["end"].startswith(
        "2300"
    )
    assert window_policy({"start": "2262-04-11T23:47:16.854775807Z", "end": "2300-01-01T00:00:00Z"})
    # A relative window reaching before year 1 cannot even be resolved.
    with pytest.raises(DatasetError, match="outside the supported range"):
        resolve_window({"last": "48h"}, datetime(1, 1, 1, tzinfo=UTC))
    now = datetime(2026, 2, 1, tzinfo=UTC)
    assert resolve_window({"last": "24h"}, now) == (now - timedelta(hours=24), now)
    assert resolve_window(window_policy(WEEK), now) == (HOUR0, HOUR0 + timedelta(days=7))


async def test_create_read_list(client, ids):
    r = await client.post(
        "/api/datasets",
        json={"name": "Meters", "series_ids": [ids["b-meter"], ids["a-meter"]], "window": {"last": "7d"}},
    )
    assert r.status_code == 201, r.text
    dataset = r.json()
    assert [s["external_id"] for s in dataset["series"]] == ["a-meter", "b-meter"]
    assert dataset["window"] == {"last": "7d"}
    assert (await client.get(f"/api/datasets/{dataset['id']}")).json() == dataset
    second = await client.post(
        "/api/datasets", json={"name": "A", "series_ids": [ids["a-meter"]], "window": WEEK}
    )
    assert second.status_code == 201
    listed = (await client.get("/api/datasets")).json()
    assert [d["name"] for d in listed["items"]] == ["A", "Meters"]
    page = (await client.get("/api/datasets", params={"limit": 1})).json()
    rest = (await client.get("/api/datasets", params={"limit": 1, "cursor": page["next_cursor"]})).json()
    assert [d["name"] for d in page["items"] + rest["items"]] == ["A", "Meters"] and rest[
        "next_cursor"
    ] is None
    assert (await client.get("/api/datasets", params={"cursor": "x"})).status_code == 422
    assert (await client.get("/api/datasets/not-a-uuid")).status_code == 404
    assert (await client.get(f"/api/datasets/{uuid.uuid4()}")).status_code == 404


async def test_422_paths(client, ids):
    async def post(**body) -> tuple[int, str, str]:
        full = {"name": "D", "series_ids": [ids["a-meter"]], "window": {"last": "7d"}, **body}
        r = await client.post("/api/datasets", json=full)
        detail = r.json()["detail"][0]
        return r.status_code, str(detail["loc"][1]), detail["msg"]

    assert await post(series_ids=[]) == (422, "series_ids", "needs 1 to 500 series, has 0")
    assert (await post(series_ids=[str(uuid.uuid4()) for _ in range(501)]))[
        2
    ] == "needs 1 to 500 series, has 501"
    ghost = str(uuid.uuid4())
    assert await post(series_ids=[ids["a-meter"], ghost]) == (422, "series_ids", f"series {ghost} not found")
    dup = ids["a-meter"]
    assert await post(series_ids=[dup, dup]) == (422, "series_ids", f"series {dup} is listed twice")
    assert await post(window={"last": "30m"}) == (422, "window", "last must be between 1h and 400d")
    assert await post(window={"start": WEEK["end"], "end": WEEK["start"]}) == (
        422,
        "window",
        "start must be before end",
    )
    assert (await post(window={"since": "x"}))[:2] == (422, "window")
    assert (await post(name=""))[:2] == (422, "name")
    assert (await post(series_ids=["nope"]))[:2] == (422, "series_ids")


async def test_patch_and_delete(client, ids):
    dataset = (
        await client.post(
            "/api/datasets", json={"name": "D", "series_ids": [ids["a-meter"]], "window": {"last": "7d"}}
        )
    ).json()
    url = f"/api/datasets/{dataset['id']}"
    r = await client.patch(
        url, json={"name": "D2", "series_ids": [ids["b-meter"], ids["a-meter"]], "window": WEEK}
    )
    assert r.status_code == 200, r.text
    patched = r.json()
    assert patched["name"] == "D2" and len(patched["series"]) == 2
    assert patched["window"] == {"start": "2026-01-01T00:00:00+00:00", "end": "2026-01-08T00:00:00+00:00"}
    bad = await client.patch(url, json={"window": {"last": "0h"}})
    assert bad.status_code == 422 and bad.json()["detail"][0]["loc"] == ["body", "window"]
    assert (await client.patch(url, json={"series_ids": None})).status_code == 422
    assert (await client.patch(f"/api/datasets/{uuid.uuid4()}", json={"name": "X"})).status_code == 404
    assert (await client.delete(url)).status_code == 204
    assert (await client.get(url)).status_code == 404
    assert (await client.delete(url)).status_code == 404
