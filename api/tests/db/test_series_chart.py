"""Chart points and on-the-fly profiles of a series (spec 025)."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tabayyun.db.models import DEFAULT_ORG_ID, DEFAULT_WORKSPACE_ID, Series
from tabayyun.main import create_app
from tabayyun.services import coverage, series_chart
from tabayyun.services.timeconv import datetime_to_ns
from tenancy import app_settings, org_session

HOUR0 = datetime(2026, 1, 1, tzinfo=UTC)
H0 = datetime_to_ns(HOUR0)
HOUR = 3600 * 10**9
BAD = range(10, 20)
log = logging.getLogger(__name__)


def quality_csv(hours: int = 72, *, bad_value: float | None = None) -> bytes:
    """Hourly values 40-60 with a `quality` column; hours 10-19 are bad (optionally `bad_value`)."""
    rows = ["ts,value,quality"]
    for h in range(hours):
        bad = h in BAD
        value = bad_value if bad and bad_value is not None else 50 + (h % 21) - 10
        rows.append(f"{(HOUR0 + timedelta(hours=h)).isoformat()},{value},{'bad' if bad else 'good'}")
    return ("\n".join(rows) + "\n").encode()


def make_app(db_url, tmp_path, **settings):
    return create_app(app_settings(db_url, inline_jobs=True, cache_url=str(tmp_path), **settings))


@pytest.fixture
def app(db_url, fresh_schema, tmp_path):
    fresh_schema("auto")
    return make_app(db_url, tmp_path)


async def _client(app):
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"X-Tabayyun-Request": "1"}
    )


@pytest.fixture
async def client(app):
    async with await _client(app) as c:
        yield c
    await app.state.engine.dispose()


async def upload(client, csv: bytes, series_id: str = "flow") -> str:
    """Upload a CSV with a quality column; return the series id."""
    r = await client.post(
        "/api/runs",
        files={"file": ("f.csv", csv, "text/csv")},
        data={"series_id": series_id, "quality_col": "quality"},
    )
    assert r.status_code == 202, r.text
    run = (await client.get(f"/api/runs/{r.json()['id']}")).json()
    assert run["status"] == "succeeded", run
    return run["series"][0]["id"]


async def test_chart_points_window_and_quality(client):
    """The default window is the extent; points, `n_raw` and the bad run come back as ns strings."""
    sid = await upload(client, quality_csv())
    r = await client.get(f"/api/series/{sid}/chart", params={"width_px": 100})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["layer"] == "raw" and body["width_px"] == 100 and body["n_raw"] == 72
    assert int(body["extent"]["start"]) == H0
    assert body["window"] == {"from": body["extent"]["start"], "to": body["extent"]["end"]}
    # 72 samples fit in 4 × 100: every sample comes back, in order, with no gap break.
    assert [int(t) for t in body["ts"]] == [H0 + h * HOUR for h in range(72)]
    assert None not in body["values"]
    [run] = body["quality"]
    assert run["quality"] == "bad"
    assert int(run["start"]) <= H0 + 10 * HOUR and H0 + 19 * HOUR < int(run["end"]) <= H0 + 20 * HOUR

    day2 = {"from": str(H0 + 24 * HOUR), "to": (HOUR0 + timedelta(hours=48)).isoformat()}
    body = (await client.get(f"/api/series/{sid}/chart", params=day2)).json()
    assert body["n_raw"] == 24 and body["quality"] == []
    assert all(H0 + 24 * HOUR <= int(t) < H0 + 48 * HOUR for t in body["ts"])


async def test_chart_downsamples_and_breaks_gaps(client):
    """Over 4 × width samples are cut to M4 points, and a missing day breaks the line."""
    rows = ["ts,value,quality"]
    for m in range(0, 6 * 24 * 60, 5):
        if 2 * 24 * 60 <= m < 3 * 24 * 60:
            continue
        rows.append(f"{(HOUR0 + timedelta(minutes=m)).isoformat()},{m % 97},good")
    sid = await upload(client, ("\n".join(rows) + "\n").encode())
    body = (await client.get(f"/api/series/{sid}/chart", params={"width_px": 100})).json()
    assert body["n_raw"] == len(rows) - 1
    assert len(body["ts"]) <= 4 * 100 + 2
    assert body["values"].count(None) == 1
    gap = body["values"].index(None)
    assert int(body["ts"][gap + 1]) - int(body["ts"][gap - 1]) >= 24 * HOUR


async def test_chart_validation_and_not_found(client):
    sid = await upload(client, quality_csv())
    url = f"/api/series/{sid}/chart"
    for params in (
        {"from": str(H0)},
        {"from": str(H0 + HOUR), "to": str(H0)},
        {"from": "yesterday", "to": str(H0)},
        {"layer": "gold"},
        {"width_px": 99},
        {"width_px": 4001},
    ):
        assert (await client.get(url, params=params)).status_code == 422, params
    assert (await client.get("/api/series/not-a-uuid/chart")).status_code == 404
    assert (await client.get("/api/series/00000000-0000-0000-0000-000000000000/chart")).status_code == 404
    assert (await client.get(f"/api/series/{sid}/profile", params={"to": str(H0)})).status_code == 422


async def test_chart_etag_and_304(client):
    """A matching `If-None-Match` is 304; a metadata change gives a new tag."""
    sid = await upload(client, quality_csv())
    url = f"/api/series/{sid}/chart"
    first = await client.get(url)
    tag = first.headers["etag"]
    assert first.headers["cache-control"] == "private, no-cache"
    again = await client.get(url, headers={"If-None-Match": tag})
    assert again.status_code == 304 and again.headers["etag"] == tag and again.content == b""
    assert (
        await client.get(url, params={"width_px": 800}, headers={"If-None-Match": tag})
    ).status_code == 200
    await client.patch(f"/api/series/{sid}", json={"expected_interval_ns": HOUR})
    changed = await client.get(url, headers={"If-None-Match": tag})
    assert changed.status_code == 200 and changed.headers["etag"] != tag


async def test_chart_and_profile_of_a_series_without_data(app, client):
    """A series the cache holds nothing of has no extent, no window and no points."""
    sid = await upload(client, quality_csv())
    async with org_session(app) as session:
        source_id = (await session.execute(select(Series.source_id))).scalars().first()
        empty = Series(
            org_id=DEFAULT_ORG_ID,
            workspace_id=DEFAULT_WORKSPACE_ID,
            source_id=source_id,
            external_id="e",
            name="e",
        )
        session.add(empty)
        await session.commit()
        empty_id = str(empty.id)
    assert sid != empty_id
    body = (await client.get(f"/api/series/{empty_id}/chart")).json()
    assert (body["extent"], body["window"], body["ts"], body["values"], body["n_raw"]) == (
        None,
        None,
        [],
        [],
        0,
    )
    prof = (await client.get(f"/api/series/{empty_id}/profile")).json()
    assert (prof["window"], prof["profile"], prof["band"]) == (None, None, None)
    assert prof["quality_counts"] == {"good": 0, "uncertain": 0, "bad": 0, "estimated": 0}


async def test_row_cap_refuses_large_windows(db_url, fresh_schema, tmp_path):
    fresh_schema("auto")
    app = make_app(db_url, tmp_path, chart_max_rows=30)
    async with await _client(app) as client:
        sid = await upload(client, quality_csv())
        r = await client.get(f"/api/series/{sid}/chart")
        assert (r.status_code, r.json()["detail"]) == (422, "window_too_large")
        assert (await client.get(f"/api/series/{sid}/profile")).status_code == 422
        small = {"from": str(H0), "to": str(H0 + 12 * HOUR)}
        assert (await client.get(f"/api/series/{sid}/chart", params=small)).json()["n_raw"] == 12
    await app.state.engine.dispose()


async def test_default_window_is_the_last_week_of_a_large_extent(client, monkeypatch):
    monkeypatch.setattr(series_chart, "DEFAULT_WHOLE_ROWS", 100)
    sid = await upload(client, quality_csv(hours=30 * 24))
    body = (await client.get(f"/api/series/{sid}/chart")).json()
    end = int(body["extent"]["end"])
    assert body["window"] == {"from": str(end - 7 * 24 * HOUR), "to": str(end)}
    assert body["n_raw"] == 7 * 24


async def test_profile_skips_bad_samples_and_learns_the_band(client):
    """Bad samples (here 1e6) stay out of the profile and band but count in quality."""
    sid = await upload(client, quality_csv(bad_value=1e6))
    body = (await client.get(f"/api/series/{sid}/profile")).json()
    assert body["quality_counts"] == {"good": 62, "uncertain": 0, "bad": 10, "estimated": 0}
    profile = body["profile"]
    assert profile["n_samples"] == 62 and profile["max"] <= 60
    band = body["band"]
    assert band["source"] == "profile"
    assert band["lo"] == pytest.approx(profile["p001"] - profile["mad"])
    assert band["hi"] == pytest.approx(profile["p999"] + profile["mad"])
    assert band["hi"] < 1000
    assert int(body["window"]["from"]) == H0


async def test_profile_band_from_metadata(client):
    sid = await upload(client, quality_csv())
    r = await client.patch(f"/api/series/{sid}", json={"operational_min": 42.0, "operational_max": 58.0})
    assert r.status_code == 200, r.text
    body = (await client.get(f"/api/series/{sid}/profile")).json()
    assert body["band"] == {"lo": 42.0, "hi": 58.0, "source": "metadata"}


async def test_chart_of_a_million_points(app, client):
    """1M raw points come back as at most 4 × width M4 points; the time is logged (spec 025)."""
    sid = await upload(client, quality_csv(hours=3))
    async with org_session(app) as session:
        series = await session.get(Series, uuid.UUID(sid))
        assert series is not None
        source_id = str(series.source_id)
    n = 1_000_000
    start = H0 + 10 * 24 * HOUR
    ts = pa.array(range(start, start + n * 10**9, 10**9), type=pa.int64()).cast(pa.timestamp("ns", tz="UTC"))
    values = pa.array([float(i % 3600) for i in range(n)])
    table = pa.table({"ts": ts, "value": values, "quality": pa.array(["good"] * n)})
    write = app.state.run_cache.write_series(
        source_id=source_id,
        series_id=sid,
        table=table,
        ts_col="ts",
        value_col="value",
        quality_col="quality",
        ingest_col=None,
    )
    async with org_session(app) as session:
        await coverage.record(
            session,
            org_id=DEFAULT_ORG_ID,
            series_id=series.id,
            start_ns=write.start_ns,
            end_ns=write.end_ns,
            rows=write.rows,
            now=datetime.now(UTC),
        )
        await session.commit()
    params = {"from": str(start), "to": str(start + n * 10**9), "width_px": 1200}
    timings = []
    for _ in range(2):
        t0 = time.perf_counter()
        r = await client.get(f"/api/series/{sid}/chart", params=params)
        timings.append(time.perf_counter() - t0)
        assert r.status_code == 200, r.text
    body = r.json()
    assert body["n_raw"] == n
    assert 0 < len(body["ts"]) <= 4 * 1200
    log.warning("chart of 1M points: %.0f ms, then %.0f ms", *(t * 1000 for t in timings))
    # Measured locally in well under 500 ms; CI runners vary, so the bound here is loose.
    assert min(timings) < 5.0
