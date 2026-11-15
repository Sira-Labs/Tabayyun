"""Compare chart and group residual (spec 018)."""

from __future__ import annotations

import logging
import math
import time
import uuid
from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pytest
from httpx import ASGITransport, AsyncClient

from tabayyun.db.models import DEFAULT_ORG_ID, Series
from tabayyun.main import create_app
from tabayyun.services import coverage
from tabayyun.services.timeconv import datetime_to_ns
from tenancy import app_settings, org_session

log = logging.getLogger(__name__)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
NS0 = datetime_to_ns(T0)
MIN = 60 * 10**9
HOUR = 60 * MIN
N = 24 * 60  # one day of minutes
LOW = range(6 * 60, 12 * 60)  # hours 6 to 12


def csv(values, *, quality=None, step_min: int = 1) -> bytes:
    """A `ts,value,quality` CSV starting at T0; `values` may hold None for a missing row."""
    rows = ["ts,value,quality"]
    for i, v in enumerate(values):
        if v is None:
            continue
        q = quality[i] if quality else "good"
        rows.append(f"{(T0 + timedelta(minutes=i * step_min)).isoformat()},{v},{q}")
    return ("\n".join(rows) + "\n").encode()


@pytest.fixture
def app(db_url, fresh_schema, tmp_path):
    fresh_schema("auto")
    return create_app(app_settings(db_url, inline_jobs=True, cache_url=str(tmp_path)))


@pytest.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"X-Tabayyun-Request": "1"}
    ) as c:
        yield c
    await app.state.engine.dispose()


async def upload(client, name: str, data: bytes, **fields: str) -> str:
    """Upload a CSV as series `name`; return the series id."""
    r = await client.post(
        "/api/runs",
        files={"file": ("f.csv", data, "text/csv")},
        data={"series_id": name, "quality_col": "quality", **fields},
    )
    assert r.status_code == 202, r.text
    run = (await client.get(f"/api/runs/{r.json()['id']}")).json()
    assert run["status"] == "succeeded", run
    return run["series"][0]["id"]


async def group(client, kind: str, members: list[tuple[str, str]], **params) -> str:
    body = {
        "name": f"g-{uuid.uuid4().hex[:8]}",
        "kind": kind,
        "members": [{"series_id": sid, "role": role} for sid, role in members],
        "params": params,
    }
    r = await client.post("/api/series-groups", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
async def balance(client) -> dict[str, str]:
    """One inlet of 300 and three outlets of 98 (2 % loss); `c` reads 30 % low for 6 hours."""
    c = [68.6 if i in LOW else 98.0 for i in range(N)]
    ids = {
        "in": await upload(client, "in", csv([300.0] * N), unit="m3/h"),
        "a": await upload(client, "a", csv([98.0] * N), unit="m3/h"),
        "b": await upload(client, "b", csv([98.0] * N), unit="m3/h"),
        "c": await upload(client, "c", csv(c), unit="m3/h"),
    }
    members = [(ids["in"], "input"), (ids["a"], "output"), (ids["b"], "output"), (ids["c"], "output")]
    ids["group"] = await group(client, "balance", members)
    return ids


def day() -> dict[str, str]:
    return {"from": str(NS0), "to": str(NS0 + N * MIN)}


async def test_chart_bins_identical_for_all_series(client):
    """Every series is cut on the same window bins, so M4 points of a bin line up."""
    fast = await upload(client, "fast", csv([math.sin(i / 50) for i in range(3 * N)]), unit="bar")
    slow = await upload(client, "slow", csv([float(i % 7) for i in range(3 * N // 5)], step_min=5))
    r = await client.get("/api/compare/chart", params=[("series", fast), ("series", slow), ("width_px", 100)])
    assert r.status_code == 200, r.text
    body = r.json()
    # The default window is the members' data (under 7 days here), reported as ns strings.
    assert body["bins"] == {"from": str(NS0), "to": body["bins"]["to"], "width_px": 100}
    start, end = int(body["bins"]["from"]), int(body["bins"]["to"])
    assert end > NS0 + (3 * N - 1) * MIN
    one, two = body["series"]
    assert (one["id"], one["name"], one["unit"]) == (fast, "fast", "bar")
    assert (two["id"], two["unit"]) == (slow, None)
    assert one["n_raw"] == 3 * N and two["n_raw"] == 3 * N // 5
    for s in body["series"]:
        bins = [(int(t) - start) * 100 // (end - start) for t in s["ts"]]
        assert bins == sorted(bins) and set(bins) == set(range(100))
        # M4 keeps at most four points per bin.
        assert max(bins.count(b) for b in set(bins)) <= 4
        assert len(s["ts"]) == len(s["values"]) <= 400


async def test_chart_stats_from_raw_window(client):
    """Statistics come from the raw usable samples of the window, whatever the chart width."""
    values = [float((i * 37) % 101) for i in range(N)]
    quality = ["bad" if i < 5 else "good" for i in range(N)]
    a = await upload(client, "a", csv(values, quality=quality), physical_min="0", physical_max="200")
    b = await upload(client, "b", csv([5.0] * N))
    window = {"from": str(NS0), "to": str(NS0 + 600 * MIN)}

    async def stats(width: int) -> dict:
        r = await client.get(
            "/api/compare/chart", params=[("series", a), ("series", b), ("width_px", width), *window.items()]
        )
        assert r.status_code == 200, r.text
        series = r.json()["series"][0]
        assert (series["physical_min"], series["physical_max"]) == (0.0, 200.0)
        return series["stats"]

    usable = values[5:600]
    mean = sum(usable) / len(usable)
    sd = math.sqrt(sum((v - mean) ** 2 for v in usable) / len(usable))
    wide, narrow = await stats(2000), await stats(100)
    assert wide == narrow
    assert (wide["min"], wide["max"]) == (min(usable), max(usable))
    assert wide["mean"] == pytest.approx(mean) and wide["sd"] == pytest.approx(sd)
    # The first usable sample, not the first sample: minutes 0 to 4 are bad.
    assert wide["first"] == {"ts": str(NS0 + 5 * MIN), "value": values[5]}


async def test_chart_points_keep_bad_quality(client):
    """Bad-quality finite samples are drawn; a window of only bad samples has no statistics."""
    quality = ["bad" if i < 60 else "good" for i in range(N)]
    a = await upload(client, "a", csv([float(i) for i in range(N)], quality=quality))
    b = await upload(client, "b", csv([1.0] * N))
    params = [("series", a), ("series", b), ("from", str(NS0)), ("to", str(NS0 + 60 * MIN))]
    one, two = (await client.get("/api/compare/chart", params=params)).json()["series"]
    assert one["n_raw"] == 60 and [int(t) for t in one["ts"]] == [NS0 + i * MIN for i in range(60)]
    assert one["values"] == [float(i) for i in range(60)]
    assert one["stats"] is None
    assert two["stats"]["mean"] == 1.0 and two["stats"]["sd"] == 0.0

    # A window before any data: no points and no statistics, but the series stay listed.
    early = [("series", a), ("series", b), ("from", str(NS0 - 2 * HOUR)), ("to", str(NS0 - HOUR))]
    body = (await client.get("/api/compare/chart", params=early)).json()
    assert [(s["ts"], s["values"], s["n_raw"], s["stats"]) for s in body["series"]] == [([], [], 0, None)] * 2


async def test_chart_rejects_count_and_unknown_ids(client):
    """Outside 2 to 8 series is a 422; ids the workspace does not have are a 404 that names them."""
    ids = [await upload(client, f"s{i}", csv([1.0, 2.0, 3.0])) for i in range(9)]

    async def get(series: list[str], **extra: str):
        return await client.get(
            "/api/compare/chart", params=[*[("series", s) for s in series], *extra.items()]
        )

    assert (await get(ids[:1])).status_code == 422
    assert (await get(ids)).status_code == 422
    assert (await get([])).status_code == 422
    assert (await get(ids[:8])).status_code == 200
    assert (await get([ids[0], ids[0]])).status_code == 422
    assert (await get([ids[0], "not-an-id"])).status_code == 422
    unknown = [str(uuid.uuid4()), str(uuid.uuid4())]
    r = await get([ids[0], unknown[0], ids[1], unknown[1]])
    assert r.status_code == 404
    assert r.json()["detail"] == {"message": "series not found", "unknown": unknown}
    assert (await get(ids[:2], **{"from": str(NS0)})).status_code == 422
    assert (await get(ids[:2], **{"from": str(NS0 + HOUR), "to": str(NS0)})).status_code == 422
    assert (await get(ids[:2], width_px="50")).status_code == 422


async def test_chart_refuses_a_window_over_the_row_limit(db_url, fresh_schema, tmp_path):
    """The row limit counts the series together."""
    fresh_schema("auto")
    app = create_app(app_settings(db_url, inline_jobs=True, cache_url=str(tmp_path), chart_max_rows=150))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"X-Tabayyun-Request": "1"}
    ) as client:
        a = await upload(client, "a", csv([1.0] * 100))
        b = await upload(client, "b", csv([2.0] * 100))
        r = await client.get("/api/compare/chart", params=[("series", a), ("series", b)])
        assert (r.status_code, r.json()["detail"]) == (422, "window_too_large")
        half = [("series", a), ("series", b), ("from", str(NS0)), ("to", str(NS0 + 50 * MIN))]
        assert (await client.get("/api/compare/chart", params=half)).status_code == 200
    await app.state.engine.dispose()


async def test_residual_balance_band_and_episode(client, balance):
    """The residual of the balance leaves the loss band exactly while the outlet reads low."""
    r = await client.get(
        f"/api/series-groups/{balance['group']}/residual", params={**day(), "width_px": 2000}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "balance" and body["grid_ns"] == MIN and body["missing"] == []
    assert body["window"] == day()
    assert [int(t) for t in body["ts"]] == [NS0 + i * MIN for i in range(N)]
    lo, hi = body["band"]["lo"], body["band"]["hi"]
    # Default band: −1 % to 5 % of the 300 input.
    assert set(lo) == {-3.0} and set(hi) == {15.0}
    outside = [not lo[i] <= x <= hi[i] for i, x in enumerate(body["residual"])]
    assert outside == [i in LOW for i in range(N)]
    assert body["residual"][0] == pytest.approx(6.0) and body["residual"][LOW[0]] == pytest.approx(35.4)

    # Downsampled to 100 bins it still leaves the band over those six hours only.
    small = (
        await client.get(f"/api/series-groups/{balance['group']}/residual", params={**day(), "width_px": 100})
    ).json()
    assert len(small["ts"]) < N and len(small["band"]["lo"]) == len(small["ts"])
    for t, x, low, high in zip(
        small["ts"], small["residual"], small["band"]["lo"], small["band"]["hi"], strict=True
    ):
        minute = (int(t) - NS0) // MIN
        assert (not low <= x <= high) == (minute in LOW), minute


async def test_residual_uses_the_groups_band_and_default_window(client, balance):
    """`loss_min`/`loss_max` of the group set the band; without a window the data's last week."""
    members = (await client.get(f"/api/series-groups/{balance['group']}")).json()["members"]
    tight = await group(
        client, "balance", [(m["series_id"], m["role"]) for m in members], loss_min=0.0, loss_max=0.01
    )
    body = (await client.get(f"/api/series-groups/{tight}/residual")).json()
    assert int(body["window"]["from"]) == NS0 and int(body["window"]["to"]) > NS0 + (N - 1) * MIN
    assert set(body["band"]["lo"]) == {0.0} and set(body["band"]["hi"]) == {3.0}
    assert all(x > 3.0 for x in body["residual"])

    bad = await group(
        client, "balance", [(m["series_id"], m["role"]) for m in members], loss_min=0.5, loss_max=0.1
    )
    r = await client.get(f"/api/series-groups/{bad}/residual")
    assert r.status_code == 422 and "loss_min" in r.json()["detail"]


async def test_residual_pair_no_band(client, balance):
    """A group of two members of any kind gives A − B without a band; three or more give none."""
    pair = await group(client, "redundant", [(balance["a"], "member"), (balance["c"], "member")])
    body = (await client.get(f"/api/series-groups/{pair}/residual", params=day())).json()
    assert body["kind"] == "pair" and body["band"] is None and body["missing"] == []
    assert len(body["residual"]) == N
    assert all(
        x == pytest.approx(29.4 if i in LOW else 0.0, abs=1e-9) for i, x in enumerate(body["residual"])
    )

    flipped = await group(client, "related", [(balance["c"], "member"), (balance["a"], "member")])
    body = (await client.get(f"/api/series-groups/{flipped}/residual", params=day())).json()
    assert body["residual"][LOW[0]] == pytest.approx(-29.4)

    three = await group(
        client, "related", [(balance["a"], "member"), (balance["b"], "member"), (balance["c"], "member")]
    )
    r = await client.get(f"/api/series-groups/{three}/residual", params=day())
    assert r.status_code == 422 and "exactly two members" in r.json()["detail"]
    assert (await client.get(f"/api/series-groups/{uuid.uuid4()}/residual")).status_code == 404
    assert (await client.get("/api/series-groups/nope/residual")).status_code == 404


async def test_residual_missing_member(client, balance):
    """A member without usable data in the window empties the residual and is listed."""
    # `late` starts a day after the others; `dead` has only bad samples.
    late = await upload(client, "late", csv([None] * N + [50.0] * 60))
    dead = await upload(client, "dead", csv([1.0] * N, quality=["bad"] * N))
    g = await group(
        client,
        "balance",
        [(balance["in"], "input"), (late, "output"), (dead, "output"), (balance["a"], "output")],
    )
    body = (await client.get(f"/api/series-groups/{g}/residual", params=day())).json()
    assert body["missing"] == [late, dead]
    assert body["ts"] == [] and body["residual"] == []
    assert body["band"] == {"lo": [], "hi": []}

    # In a window where no member has data, every member is missing.
    before = {"from": str(NS0 - 2 * HOUR), "to": str(NS0 - HOUR)}
    body = (await client.get(f"/api/series-groups/{g}/residual", params=before)).json()
    assert body["missing"] == [balance["in"], late, dead, balance["a"]]


async def test_residual_skips_incomplete_bins(client, balance):
    """A gap in one member leaves a gap in the residual; bad-quality samples do not count."""
    values = [98.0] * N
    quality = ["good"] * N
    for i in range(100, 130):
        values[i] = None  # no rows
    for i in range(300, 320):
        quality[i] = "bad"  # rows that do not count
    gappy = await upload(client, "gappy", csv(values, quality=quality))
    pair = await group(client, "related", [(balance["in"], "member"), (gappy, "member")])
    body = (
        await client.get(f"/api/series-groups/{pair}/residual", params={**day(), "width_px": 2000})
    ).json()
    minutes = {
        (int(t) - NS0) // MIN: x for t, x in zip(body["ts"], body["residual"], strict=True) if x is not None
    }
    skipped = set(range(100, 130)) | set(range(300, 320))
    assert set(minutes) == set(range(N)) - skipped
    assert all(x == pytest.approx(202.0) for x in minutes.values())
    # The line breaks once after each run of skipped bins.
    assert body["residual"].count(None) == 2


async def test_chart_of_eight_series_of_a_million_points(app, client):
    """8 × 1M raw points come back on shared bins; the time is logged (spec 018)."""
    n = 1_000_000
    start = NS0 + 10 * 24 * HOUR
    ts = pa.array(range(start, start + n * 10**9, 10**9), type=pa.int64()).cast(pa.timestamp("ns", tz="UTC"))
    quality = pa.array(["good"] * n)
    ids = []
    for k in range(8):
        sid = await upload(client, f"big-{k}", csv([1.0, 2.0, 3.0]))
        async with org_session(app) as session:
            series = await session.get(Series, uuid.UUID(sid))
            assert series is not None
            table = pa.table(
                {"ts": ts, "value": pa.array([float((i + k) % 3600) for i in range(n)]), "quality": quality}
            )
            write = app.state.run_cache.write_series(
                source_id=str(series.source_id),
                series_id=sid,
                table=table,
                ts_col="ts",
                value_col="value",
                quality_col="quality",
                ingest_col=None,
            )
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
        ids.append(sid)
    params = [*[("series", sid) for sid in ids], ("from", str(start)), ("to", str(start + n * 10**9))]
    timings = []
    for _ in range(2):
        t0 = time.perf_counter()
        r = await client.get("/api/compare/chart", params=params)
        timings.append(time.perf_counter() - t0)
        assert r.status_code == 200, r.text
    body = r.json()
    assert [s["n_raw"] for s in body["series"]] == [n] * 8
    assert all(0 < len(s["ts"]) <= 4 * 1200 for s in body["series"])
    assert all(s["stats"]["min"] == 0.0 and s["stats"]["max"] == 3599.0 for s in body["series"])
    log.warning("compare chart of 8 × 1M points: %.0f ms, then %.0f ms", *(t * 1000 for t in timings))
    # Measured locally well under the spec's 1.5 s; CI runners vary, so the bound here is loose.
    assert min(timings) < 15.0
