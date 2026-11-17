"""Baseline profiles (spec 028): what they are computed from, versions, routes, the scheduler
and their use by dataset runs."""

from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from admin_support import ALICE, MIA
from tabayyun.db import for_org
from tabayyun.db.models import DEFAULT_ORG_ID, SeriesBaseline
from tabayyun.services import baselines
from tabayyun.services.timeconv import datetime_to_ns

T0 = datetime(2026, 3, 1, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
DAY_NS = 86_400 * 10**9


def _noise(i: int) -> float:
    """Deterministic noise in [-0.5, 0.5)."""
    return ((i * 2654435761) % 2**32) / 2**32 - 0.5  # Knuth's multiplicative hash


def _rows(n: int, *, start: datetime = T0, step: timedelta = MINUTE, scale: float = 1.0) -> list[list]:
    return [
        [start + i * step, 50 + 10 * math.sin(i / 240 * 2 * math.pi) + scale * _noise(i), "good"]
        for i in range(n)
    ]


async def _upload(client, name: str, rows: list[list], **data) -> dict:
    """Upload rows of `(ts, value, quality)` as series `name`; the finished run."""
    lines = ["ts,value,quality"] + [
        f"{ts.isoformat()},{'' if value != value else f'{value:.4f}'},{quality}"  # NaN: an empty cell
        for ts, value, quality in rows
    ]
    r = await client.post(
        "/api/runs",
        files={"file": ("f.csv", ("\n".join(lines) + "\n").encode(), "text/csv")},
        data={"series_id": name, "quality_col": "quality"} | {k: str(v) for k, v in data.items()},
    )
    assert r.status_code == 202, r.text
    run = (await client.get(f"/api/runs/{r.json()['id']}")).json()
    assert run["status"] == "succeeded" and run["stats"]["cache"]["written"], run
    return run


class Lab:
    """A client acting as Alice with a local cache, and the service called as the worker would."""

    def __init__(self, admin_env, tmp_path, **settings) -> None:
        self.env = admin_env
        self.client = admin_env.client(ALICE, cache_url=str(tmp_path), **settings)
        self.app = admin_env.apps[-1]
        self.factory = for_org(self.app.state.session_factory, DEFAULT_ORG_ID)

    async def series(self, name: str, rows: list[list], **data) -> uuid.UUID:
        return uuid.UUID((await _upload(self.client, name, rows, **data))["series"][0]["id"])

    async def compute(self, series_id: uuid.UUID, *, max_rows: int = 5_000_000, trigger="schedule"):
        return await baselines.compute_baseline(
            self.factory, self.app.state.run_cache, series_id, trigger=trigger, max_rows=max_rows
        )

    async def rows(self, series_id: uuid.UUID) -> list[SeriesBaseline]:
        async with self.factory() as session:
            stmt = select(SeriesBaseline).where(SeriesBaseline.series_id == series_id)
            return list((await session.execute(stmt.order_by(SeriesBaseline.version))).scalars())

    async def listed(self, series_id: uuid.UUID) -> list[dict]:
        r = await self.client.get(f"/api/series/{series_id}/baselines")
        assert r.status_code == 200, r.text
        assert r.json()["next_cursor"] is None
        return r.json()["items"]


@pytest.fixture
async def lab(admin_env, tmp_path):
    lab = Lab(admin_env, tmp_path)
    async with lab.client:
        yield lab


async def test_first_baseline_and_too_few_samples(lab):
    sid = await lab.series("flow", _rows(3 * 1440))
    assert await lab.compute(sid) == baselines.Outcome("stored", 1)
    [row] = await lab.rows(sid)
    assert (row.version, row.trigger, row.n_samples) == (1, "schedule", 3 * 1440)
    # Three days of data: the window is all the cache holds, up to just after the last sample.
    last = T0 + (3 * 1440 - 1) * MINUTE
    assert row.window_start == T0 and last < row.window_end <= last + timedelta(microseconds=1)
    assert row.excluded == {"bad": 0, "findings": 0, "windows": []}
    assert row.profile["n_samples"] == 3 * 1440 and row.profile["expected_interval_ns"] == 60 * 10**9
    assert (row.org_id, row.series_id) == (DEFAULT_ORG_ID, sid)

    # Forty days of hourly data: the trailing 28 days.
    long = await lab.series("level", _rows(40 * 24, step=timedelta(hours=1)))
    assert (await lab.compute(long)).status == "stored"
    [row] = await lab.rows(long)
    assert row.window_end - row.window_start == timedelta(days=28)
    assert row.n_samples == 28 * 24

    few = await lab.series("few", _rows(baselines.MIN_SAMPLES - 1))
    assert await lab.compute(few) == baselines.Outcome("too_few")
    assert await lab.rows(few) == []
    assert await lab.compute(uuid.uuid4()) == baselines.Outcome("gone")


async def test_exclusions_bad_quality_and_open_critical_findings(lab):
    rows = _rows(2 * 1440)
    for i in range(100, 110):
        rows[i][2] = "bad"
        rows[i][1] = 5000.0  # nonsense values the meter itself flags
    for i in range(700, 720):
        rows[i][1] = 400.0  # above the physical maximum: a critical finding
    run = await _upload(lab.client, "pressure", rows, physical_max=200)
    sid = uuid.UUID(run["series"][0]["id"])
    found = (await lab.client.get("/api/findings", params={"series_id": str(sid), "status": "all"})).json()[
        "items"
    ]
    critical = [f for f in found if f["severity"] == "critical"]
    assert critical and {f["check_id"] for f in critical} == {"tby.physical_range"}
    assert any(f["severity"] != "critical" for f in found), "a non-critical finding is needed too"

    assert (await lab.compute(sid)).status == "stored"
    [row] = await lab.rows(sid)
    assert row.excluded["bad"] == 10 and row.excluded["findings"] == 20
    assert row.n_samples == 2 * 1440 - 30
    spike = (datetime_to_ns(T0 + 700 * MINUTE), datetime_to_ns(T0 + 719 * MINUTE))
    # Only the critical findings' windows, and together they cover the excursion.
    assert len(row.excluded["windows"]) == len(critical)
    assert min(w[0] for w in row.excluded["windows"]) <= spike[0]
    assert max(w[1] for w in row.excluded["windows"]) > spike[1]
    assert row.profile["max"] < 70  # neither the excursion nor the bad samples were learned

    # Resolved, the excursion counts as behaviour again; bad samples stay out.
    for finding in critical:
        r = await lab.client.patch(f"/api/findings/{finding['id']}", json={"status": "resolved"})
        assert r.status_code == 200, r.text
    assert await lab.compute(sid) == baselines.Outcome("stored", 2)
    newest = (await lab.rows(sid))[-1]
    assert newest.excluded == {"bad": 10, "findings": 0, "windows": []}
    assert newest.profile["max"] == 400.0 and newest.n_samples == 2 * 1440 - 10


async def test_versions_no_change_new_data_pruning(lab, monkeypatch):
    sid = await lab.series("flow", _rows(1440))
    assert await lab.compute(sid) == baselines.Outcome("stored", 1)
    assert await lab.compute(sid, trigger="manual") == baselines.Outcome("unchanged", 1)
    assert [(r.version, r.trigger) for r in await lab.rows(sid)] == [(1, "schedule")]

    await lab.series("flow", _rows(1440, start=T0 + timedelta(days=1), scale=4.0))
    assert await lab.compute(sid, trigger="manual") == baselines.Outcome("stored", 2)
    first, second = await lab.rows(sid)
    assert (second.version, second.trigger, second.n_samples) == (2, "manual", 2 * 1440)
    assert second.profile["noise_mad"] > first.profile["noise_mad"]

    monkeypatch.setattr(baselines, "KEEP_VERSIONS", 2)
    await lab.series("flow", _rows(1440, start=T0 + timedelta(days=2)))
    assert await lab.compute(sid) == baselines.Outcome("stored", 3)
    assert [r.version for r in await lab.rows(sid)] == [2, 3]


async def test_window_shrinks_to_the_row_limit(lab):
    sid = await lab.series("level", _rows(40 * 24, step=timedelta(hours=1)))
    # 28 days hold 672 rows and 14 days 336: the first window that fits 400 rows.
    assert (await lab.compute(sid, max_rows=400)).status == "stored"
    [row] = await lab.rows(sid)
    assert row.window_end - row.window_start == timedelta(days=14)
    assert row.n_samples == 14 * 24
    assert await lab.compute(sid, max_rows=10) == baselines.Outcome("too_large")
    assert len(await lab.rows(sid)) == 1


async def test_thresholds_follow_the_profile(lab):
    sid = await lab.series("flow", _rows(1440))
    await lab.compute(sid)
    [one] = await lab.listed(sid)
    profile = one["profile"]
    assert (one["version"], one["trigger"], one["n_samples"]) == (1, "schedule", 1440)
    assert one["window"]["from"] == str(datetime_to_ns(T0))
    assert one["excluded"] == {"bad": 0, "findings": 0, "windows": []}
    thresholds = {(t["check_id"], t["param"]): t for t in one["thresholds"]}
    assert set(thresholds) == {("tby.rate_of_change", "max_rate"), ("tby.spikes", "min_scale")}
    rate = thresholds["tby.rate_of_change", "max_rate"]
    assert rate["value"] == pytest.approx(3.0 * profile["rate_p999"]) and "99.9th percentile" in rate["basis"]
    floor = thresholds["tby.spikes", "min_scale"]
    assert floor["value"] == pytest.approx(max(profile["resolution"], profile["noise_mad"]))
    assert one["band"] == {
        "lo": pytest.approx(profile["p001"] - profile["mad"]),
        "hi": pytest.approx(profile["p999"] + profile["mad"]),
        "source": "profile",
    }

    # A noisier day arrives: the refreshed baseline gives looser thresholds.
    await lab.series("flow", _rows(1440, start=T0 + timedelta(days=1), scale=6.0))
    await lab.compute(sid, trigger="manual")
    two, old = await lab.listed(sid)
    assert (two["version"], old["version"], old["thresholds"]) == (2, 1, one["thresholds"])
    new = {(t["check_id"], t["param"]): t["value"] for t in two["thresholds"]}
    assert new["tby.rate_of_change", "max_rate"] > rate["value"]
    assert new["tby.spikes", "min_scale"] > floor["value"]
    limited = (await lab.client.get(f"/api/series/{sid}/baselines", params={"limit": 1})).json()["items"]
    assert [b["version"] for b in limited] == [2]

    # The series' declared resolution wins over the learned one where it is larger.
    r = await lab.client.patch(f"/api/series/{sid}", json={"resolution": 25.0})
    assert r.status_code == 200, r.text
    declared = {(t["check_id"], t["param"]): t["value"] for t in (await lab.listed(sid))[0]["thresholds"]}
    assert declared["tby.spikes", "min_scale"] == 25.0


async def test_routes_roles_no_data_and_audit(admin_env, tmp_path):
    settings = {"cache_url": str(tmp_path)}
    async with admin_env.client(ALICE, **settings) as alice:
        sid = (await _upload(alice, "flow", _rows(1440)))["series"][0]["id"]
        assert (await alice.get(f"/api/series/{sid}/baselines")).json() == {"items": [], "next_cursor": None}
        r = await alice.post(f"/api/series/{sid}/baselines")
        assert (r.status_code, r.json()) == (202, {"queued": True})
        # Inline jobs: computed after the response.
        [baseline] = (await alice.get(f"/api/series/{sid}/baselines")).json()["items"]
        assert (baseline["version"], baseline["trigger"]) == (1, "manual")

        # A series the cache holds nothing of.
        source = await alice.post("/api/sources", json={"type": "synthetic", "name": "plant"})
        r = await alice.post(
            f"/api/sources/{source.json()['id']}/series", json=[{"external_id": "p1", "name": "p1"}]
        )
        assert r.status_code in (200, 201), r.text
        empty = admin_env.scalar("SELECT id FROM series WHERE external_id = 'p1'")
        r = await alice.post(f"/api/series/{empty}/baselines")
        assert (r.status_code, r.json()["detail"]) == (409, "no_data")
        assert (await alice.get(f"/api/series/{empty}/baselines")).json()["items"] == []
        for missing in (uuid.uuid4(), "nope"):
            assert (await alice.get(f"/api/series/{missing}/baselines")).status_code == 404
            assert (await alice.post(f"/api/series/{missing}/baselines")).status_code == 404
        assert (await alice.get(f"/api/series/{sid}/baselines", params={"limit": 0})).status_code == 422
    async with admin_env.client(MIA, **settings) as mia:
        assert [b["version"] for b in (await mia.get(f"/api/series/{sid}/baselines")).json()["items"]] == [1]
        assert (await mia.post(f"/api/series/{sid}/baselines")).status_code == 403
    [event] = admin_env.events("series.baseline_refresh_requested")
    assert str(event.target_id) == sid and event.details == {"name": "flow"}

    # Without inline jobs the request queues the job under the shared lock.
    async with admin_env.client(ALICE, inline_jobs=False, **settings) as alice:
        assert (await alice.post(f"/api/series/{sid}/baselines")).status_code == 202
    [job] = admin_env.sql("SELECT queue_name, task_name, lock, args FROM procrastinate_jobs")
    assert (job.queue_name, job.task_name, job.lock) == (
        "maintenance",
        "tabayyun.profile_series",
        "baselines",
    )
    assert job.args == {"series_id": sid, "org_id": str(DEFAULT_ORG_ID), "trigger": "manual"}


async def test_scheduler_claims_once_a_day_with_the_lock(lab):
    a = await lab.series("a", _rows(200))
    b = await lab.series("b", _rows(200))
    source = await lab.client.post("/api/sources", json={"type": "synthetic", "name": "plant"})
    await lab.client.post(
        f"/api/sources/{source.json()['id']}/series", json=[{"external_id": "p1", "name": "p1"}]
    )
    all_orgs = lab.app.state.session_factory
    now = datetime(2026, 6, 1, 2, 37, tzinfo=UTC)

    def jobs() -> list[dict]:
        rows = lab.env.sql(
            "SELECT lock, queue_name, args FROM procrastinate_jobs "
            "WHERE task_name = 'tabayyun.profile_series' ORDER BY id"
        )
        assert {(r.lock, r.queue_name) for r in rows} <= {("baselines", "maintenance")}
        return [r.args for r in rows]

    # One per tick when the limit says so, the series that waited longest first.
    assert await baselines.queue_due(all_orgs, now, limit=1) == 1
    assert await baselines.queue_due(all_orgs, now + timedelta(minutes=1)) == 1
    queued = jobs()
    assert {j["series_id"] for j in queued} == {str(a), str(b)}  # not the series without data
    assert all(
        j == {"series_id": j["series_id"], "org_id": str(DEFAULT_ORG_ID), "trigger": "schedule"}
        for j in queued
    )
    first = uuid.UUID(queued[0]["series_id"])
    assert await baselines.queue_due(all_orgs, now + timedelta(hours=23)) == 0
    assert await baselines.queue_due(all_orgs, now + timedelta(hours=24)) == 1
    assert jobs()[-1]["series_id"] == str(first)
    assert await baselines.queue_due(all_orgs, now + timedelta(hours=24, minutes=1)) == 1
    assert len(jobs()) == 4
    # The claim does not count as a change of the series.
    stamps = lab.env.sql("SELECT baseline_checked_at, updated_at, created_at FROM series WHERE id = :i", i=a)
    assert stamps[0].baseline_checked_at is not None and stamps[0].updated_at == stamps[0].created_at


async def test_dataset_run_uses_the_stored_baseline(lab):
    sid = await lab.series("flow", _rows(2 * 1440))
    window = {"start": T0.isoformat(), "end": (T0 + timedelta(days=2)).isoformat()}
    dataset = await lab.client.post(
        "/api/datasets", json={"name": "D", "series_ids": [str(sid)], "window": window}
    )
    assert dataset.status_code == 201, dataset.text

    async def run() -> tuple[dict, set[str]]:
        r = await lab.client.post("/api/runs", json={"dataset_id": dataset.json()["id"]})
        finished = (await lab.client.get(f"/api/runs/{r.json()['id']}")).json()
        assert finished["status"] == "succeeded", finished
        found = (
            await lab.client.get("/api/findings", params={"series_id": str(sid), "status": "all"})
        ).json()
        return finished, {f["check_id"] for f in found["items"] if f["last_run_id"] == finished["id"]}

    # Without a baseline the series is judged against its own window: ordinary steps are usual.
    plain, checks = await run()
    assert plain["stats"]["baselines"] == {} and "tby.rate_of_change" not in checks

    # A baseline from a much calmer time: the same steps now exceed three times its usual rate.
    assert (await lab.compute(sid)).status == "stored"
    async with lab.factory() as session, session.begin():
        [row] = (await session.execute(select(SeriesBaseline))).scalars()
        calm = dict(row.profile, rate_p999=row.profile["rate_p999"] / 1000)
        await session.execute(
            text("UPDATE series_baselines SET profile = CAST(:p AS jsonb) WHERE id = :id"),
            {"p": json.dumps(calm), "id": row.id},
        )
    judged, checks = await run()
    assert judged["stats"]["baselines"] == {str(sid): 1}
    assert "tby.rate_of_change" in checks


async def test_point_finding_at_the_first_sample_is_left_out(lab):
    """A critical finding at one instant has an empty window; at the baseline's first sample it
    still leaves that sample out."""
    rows = _rows(1440)
    rows[0][1] = 400.0  # the first sample alone is above the physical maximum
    run = await _upload(lab.client, "pressure", rows, physical_max=200)
    sid = uuid.UUID(run["series"][0]["id"])
    # Make it a finding at exactly that instant, as point checks report them.
    lab.env.sql(
        "UPDATE findings SET window_start = :t, window_end = :t "
        "WHERE series_id = :s AND severity = 'critical'",
        t=T0,
        s=sid,
    )
    assert lab.env.scalar(
        "SELECT count(*) FROM findings WHERE series_id = :s AND window_end = :t", s=sid, t=T0
    )
    assert (await lab.compute(sid)).status == "stored"
    [row] = await lab.rows(sid)
    t0 = datetime_to_ns(T0)
    assert row.excluded == {"bad": 0, "findings": 1, "windows": [[t0, t0 + 1]]}
    assert row.n_samples == 1439 and row.profile["max"] < 70


async def test_rows_read_are_bounded_not_only_estimated(lab):
    """Coverage spreads a write's rows evenly over its span: a dense burst inside a long write
    makes a window look smaller than it is. The limit holds for the rows that were read."""
    sparse = _rows(30, step=timedelta(days=1))  # one sample a day for 30 days...
    burst = _rows(2000, start=T0 + timedelta(days=29, hours=1), step=timedelta(seconds=1))
    sid = await lab.series("bursty", sorted(sparse + burst, key=lambda r: r[0]))
    # ...so 2030 rows over 30 days look like about 68 for the last day, which holds 2001.
    assert await lab.compute(sid, max_rows=1900) == baselines.Outcome("too_large")
    assert await lab.rows(sid) == []
    assert (await lab.compute(sid, max_rows=2500)).status == "stored"


async def test_an_older_computation_does_not_land_on_a_newer_one(lab, monkeypatch):
    """A refresh that finishes while another computation of the series is reading: the slower
    one notices the new version and computes again from the newer state."""
    sid = await lab.series("flow", _rows(1440))
    real_read = baselines.series_chart.read_window
    calls = 0

    async def slow_read(cache, series, start_ns, end_ns):
        nonlocal calls
        calls += 1
        batch = await real_read(cache, series, start_ns, end_ns)
        if calls == 1:
            # While the first computation holds day one in memory, day two arrives and a
            # refresh from the page stores version 1 from both days.
            await lab.series("flow", _rows(1440, start=T0 + timedelta(days=1), scale=4.0))
            assert await lab.compute(sid, trigger="manual") == baselines.Outcome("stored", 1)
        return batch

    monkeypatch.setattr(baselines.series_chart, "read_window", slow_read)
    # Its day-one result is not stored on top: it reads again, and finds nothing new to store.
    assert await lab.compute(sid) == baselines.Outcome("unchanged", 1)
    [row] = await lab.rows(sid)
    assert (row.version, row.trigger, row.n_samples) == (1, "manual", 2 * 1440)

    # Stores that keep arriving: it gives up after a few attempts and leaves the newest alone.
    async def always_behind(cache, series, start_ns, end_ns):
        batch = await real_read(cache, series, start_ns, end_ns)
        lab.env.sql("UPDATE series_baselines SET version = version + 1 WHERE series_id = :s", s=sid)
        return batch

    monkeypatch.setattr(baselines.series_chart, "read_window", always_behind)
    assert await lab.compute(sid) == baselines.Outcome("superseded")
    assert [r.version for r in await lab.rows(sid)] == [1 + baselines.MAX_ATTEMPTS]


async def test_a_store_between_the_reads_of_the_inputs_is_noticed(lab, monkeypatch):
    """The inputs are read statement by statement. A newer baseline stored after the version
    was read but before the extent leaves the computation with a stale extent: its result must
    not be stored as the newest version."""
    sid = await lab.series("flow", _rows(1440))
    real_extent = baselines.series_chart.extent
    calls = 0

    async def stale_extent(session, series_id, *args):
        nonlocal calls
        calls += 1
        ext = await real_extent(session, series_id, *args)  # day one only
        if calls == 1:
            await lab.series("flow", _rows(1440, start=T0 + timedelta(days=1), scale=4.0))
            assert await lab.compute(sid, trigger="manual") == baselines.Outcome("stored", 1)
        return ext

    monkeypatch.setattr(baselines.series_chart, "extent", stale_extent)
    assert await lab.compute(sid) == baselines.Outcome("unchanged", 1)
    [row] = await lab.rows(sid)
    assert (row.version, row.trigger, row.n_samples) == (1, "manual", 2 * 1440)


async def test_samples_without_a_number_do_not_count(lab):
    """Empty cells arrive as NaN. They are not learned from and not counted: neither towards
    the minimum nor as bad quality."""
    rows = _rows(1440)
    for i in range(200, 500):
        rows[i][1] = float("nan")
    rows[600][2] = "bad"
    sid = await lab.series("gappy", rows)
    assert (await lab.compute(sid)).status == "stored"
    [row] = await lab.rows(sid)
    assert row.n_samples == 1440 - 300 - 1 == row.profile["n_finite"]
    assert row.excluded == {"bad": 1, "findings": 0, "windows": []}

    # Sixty samples of which twenty hold a number: too few, although sixty rows were read.
    sparse = _rows(60)
    for i in range(20, 60):
        sparse[i][1] = float("nan")
    few = await lab.series("mostly-empty", sparse)
    assert await lab.compute(few) == baselines.Outcome("too_few")
