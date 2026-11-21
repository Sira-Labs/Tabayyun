"""The overview against the database (spec 032): the daily scores a run keeps, the tiles and
their trend, the worst series, findings per day, filters, tenancy and speed."""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta

from admin_support import BOB, MIA, NED, ORG_B
from synth_csv import faulty_csv
from tabayyun.authz.scope import Scope
from tabayyun.db.models import DEFAULT_ORG_ID, DEFAULT_WORKSPACE_ID
from tabayyun.services import overview
from tenancy import org_session

SCOPE = Scope(org_id=DEFAULT_ORG_ID, workspace_id=DEFAULT_WORKSPACE_ID)
TODAY = datetime.now(UTC).date()
LAST_FETCH = {"id": "f1", "status": "succeeded", "finished_at": "2026-11-20T10:00:00+00:00", "rows": 12}
NOON = datetime(TODAY.year, TODAY.month, TODAY.day, 12, 0, tzinfo=UTC)


def day(back: int) -> datetime:
    """Noon UTC, `back` days ago."""
    return NOON - timedelta(days=back)


class World:
    """Series made through the API with nothing scored, and score days and findings written
    as the owner, so each test decides exactly what the overview reads."""

    def __init__(self, env, client) -> None:
        self.env, self.client = env, client

    async def series(self, name: str, asset_path: str | None = None) -> str:
        rows = "ts,value\n" + "".join(f"2026-01-01T{h:02d}:00:00Z,{h}\n" for h in range(24))
        r = await self.client.post(
            "/api/runs", files={"file": ("f.csv", rows.encode(), "text/csv")}, data={"series_id": name}
        )
        run = (await self.client.get(f"/api/runs/{r.json()['id']}")).json()
        assert run["status"] == "succeeded", run
        sid = run["series"][0]["id"]
        if asset_path is not None:
            patched = await self.client.patch(f"/api/series/{sid}", json={"asset_path": asset_path})
            assert patched.status_code == 200
        for table in ("findings", "scores", "score_days"):
            self.env.sql(f"DELETE FROM {table} WHERE series_id = :s", s=sid)  # noqa: S608
        return sid

    def score(self, series: str, back: int, overall: float, **dimensions: float) -> None:
        at = day(back)
        columns = "".join(f", {d}" for d in dimensions)
        values = "".join(f", :{d}" for d in dimensions)
        self.env.sql(
            "INSERT INTO score_days (org_id, workspace_id, series_id, layer, day, overall, n_findings, "  # noqa: S608
            f"computed_at{columns}) VALUES (:o, :w, :s, 'raw', :d, :v, 0, :at{values})",
            o=DEFAULT_ORG_ID,
            w=DEFAULT_WORKSPACE_ID,
            s=series,
            d=at.date(),
            v=overall,
            at=at,
            **dimensions,
        )

    def finding(self, series: str, back: int, severity: str, status: str = "open") -> None:
        at = day(back)
        self.env.sql(
            "INSERT INTO findings (org_id, workspace_id, id, series_id, check_id, dimension, severity, "
            "window_start, window_end, summary, status, created_at) VALUES (:o, :w, gen_random_uuid(), :s, "
            "'tby.flatline', 'plausibility', :sev, :a, :b, 'x', :st, :at)",
            o=DEFAULT_ORG_ID,
            w=DEFAULT_WORKSPACE_ID,
            s=series,
            sev=severity,
            a=at - timedelta(hours=1),
            b=at,
            st=status,
            at=at,
        )

    async def get(self, **params) -> dict:
        r = await self.client.get("/api/overview", params=params)
        assert r.status_code == 200, r.text
        return r.json()


def tile(body: dict, metric: str) -> dict:
    return next(t for t in body["tiles"] if t["metric"] == metric)


async def test_run_writes_its_score_day(admin_env):
    """A run keeps its score as the series' score of that day; a later run the same day
    replaces it, an older score does not, and another day adds a row."""
    csv = faulty_csv(["gap", "flatline"])
    async with admin_env.client() as c:
        for _ in range(2):
            r = await c.post(
                "/api/runs", files={"file": ("f.csv", csv, "text/csv")}, data={"series_id": "demo"}
            )
            run = (await c.get(f"/api/runs/{r.json()['id']}")).json()
            assert run["status"] == "succeeded"
        sid = run["series"][0]["id"]
        rows = admin_env.sql(
            "SELECT day, overall, completeness, plausibility, n_findings, computed_at, workspace_id "
            "FROM score_days WHERE series_id = :s",
            s=sid,
        )
        newest = admin_env.sql(
            "SELECT overall, dimensions, n_findings, computed_at FROM scores WHERE series_id = :s "
            "ORDER BY computed_at DESC LIMIT 1",
            s=sid,
        )[0]
        assert len(rows) == 1
        kept = rows[0]
        assert (kept[0], kept[1], kept[4], kept[5]) == (newest[3].date(), newest[0], newest[2], newest[3])
        assert (kept[2], kept[3]) == (newest[1].get("completeness"), newest[1].get("plausibility"))
        assert kept[2] is not None and kept[2] < 100 and kept[6] == DEFAULT_WORKSPACE_ID

        app = admin_env.apps[-1]
        # Older, but on the same UTC day whatever the time of this test: the day's first moment.
        earlier = newest[3].astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        next_day = newest[3] + timedelta(days=1)
        async with org_session(app) as session, session.begin():
            for at, overall in ((earlier, 1.0), (next_day, 2.0)):
                await overview.record_score_day(
                    session,
                    SCOPE,
                    series_id=uuid.UUID(sid),
                    overall=overall,
                    dimensions={"validity": 3.0, "unknown": 4.0},
                    n_findings=7,
                    computed_at=at,
                )
        after = admin_env.sql(
            "SELECT day, overall, validity, completeness FROM score_days WHERE series_id = :s ORDER BY day",
            s=sid,
        )
        assert [tuple(r) for r in after] == [
            (newest[3].date(), newest[0], kept_validity(admin_env, sid, newest[3]), kept[2]),
            (next_day.astimezone(UTC).date(), 2.0, 3.0, None),
        ]


def kept_validity(env, sid: str, at: datetime) -> float | None:
    return env.scalar("SELECT validity FROM score_days WHERE series_id = :s AND day = :d", s=sid, d=at.date())


async def test_overview_tiles_and_trend(admin_env):
    """Tiles hold the mean, minimum and count over each series' newest day in the window; the
    trend has a point per day with the series scored that day."""
    async with admin_env.client() as c:
        world = World(admin_env, c)
        a, b, idle = await world.series("a"), await world.series("b"), await world.series("idle")
        world.score(a, 0, 90.0, completeness=80.0, validity=100.0)
        world.score(a, 3, 70.0, completeness=60.0)
        world.score(b, 3, 50.0, completeness=40.0)
        world.score(b, 40, 10.0, completeness=10.0)  # outside 30 days
        world.score(idle, 60, 99.0)
        body = await world.get()
        assert body["window"] == {
            "days": 30,
            "start": (TODAY - timedelta(days=29)).isoformat(),
            "end": TODAY.isoformat(),
        }
        assert body["series"] == {"total": 3, "scored": 2}
        assert [t["metric"] for t in body["tiles"]] == list(overview.METRICS)
        overall, completeness = tile(body, "overall"), tile(body, "completeness")
        assert (overall["mean"], overall["min"], overall["n_series"]) == (70.0, 50.0, 2)
        assert (completeness["mean"], completeness["min"], completeness["n_series"]) == (60.0, 40.0, 2)
        assert (tile(body, "validity")["mean"], tile(body, "validity")["n_series"]) == (100.0, 1)
        assert tile(body, "timeliness") == {
            "metric": "timeliness",
            "mean": None,
            "min": None,
            "n_series": 0,
            "trend": [],
        }
        assert overall["trend"] == [
            {"day": day(3).date().isoformat(), "mean": 60.0, "n": 2},
            {"day": TODAY.isoformat(), "mean": 90.0, "n": 1},
        ]
        assert tile(body, "validity")["trend"] == [{"day": TODAY.isoformat(), "mean": 100.0, "n": 1}]

        # A day after the window's end (a clock ahead, a run finishing past midnight while the
        # request is answered) belongs to neither the tiles nor the trend.
        world.score(a, -1, 5.0, completeness=5.0)
        world.finding(a, -1, "critical")
        ahead = await world.get()
        assert tile(ahead, "overall") == overall and tile(ahead, "completeness") == completeness
        assert ahead["findings"]["days"][-1]["day"] == TODAY.isoformat()
        assert sum(d["critical"] for d in ahead["findings"]["days"]) == 0

        week = await world.get(days=7)
        assert week["window"]["days"] == 7 and week["series"]["scored"] == 2
        quarter = await world.get(days=90)
        assert quarter["series"]["scored"] == 3
        assert tile(quarter, "overall")["trend"][0] == {
            "day": day(60).date().isoformat(),
            "mean": 99.0,
            "n": 1,
        }
        assert tile(quarter, "overall")["n_series"] == 3


async def test_worst_series_and_open_findings(admin_env):
    """The worst list ranks by the lowest value, names that metric and counts what is unresolved."""
    async with admin_env.client() as c:
        world = World(admin_env, c)
        ids = {name: await world.series(name, f"plant/{name}") for name in ("fine", "hidden", "poor")}
        world.score(ids["fine"], 0, 99.0, completeness=99.0)
        world.score(ids["hidden"], 1, 95.0, completeness=41.0, validity=100.0)
        world.score(ids["poor"], 0, 60.0, completeness=70.0)
        world.finding(ids["hidden"], 2, "high")
        world.finding(ids["hidden"], 2, "low", status="acked")
        world.finding(ids["hidden"], 2, "low", status="resolved")
        body = await world.get()
        assert (body["worst"]["share"], body["worst"]["n"]) == (0.05, 1)
        [item] = body["worst"]["items"]
        assert item == {
            "series_id": ids["hidden"],
            "name": "hidden",
            "asset_path": "plant/hidden",
            "overall": 95.0,
            "metric": "completeness",
            "value": 41.0,
            "open_findings": 2,
            "computed_at": item["computed_at"],
        }
        assert datetime.fromisoformat(item["computed_at"]) == day(1)


async def test_findings_per_day(admin_env):
    """New findings are counted on the day they were first stored, every day of the window is
    there, and the unresolved ones are counted by severity whatever their age."""
    async with admin_env.client() as c:
        world = World(admin_env, c)
        a = await world.series("a")
        world.finding(a, 0, "critical")
        world.finding(a, 0, "low", status="resolved")
        world.finding(a, 2, "high")
        world.finding(a, 2, "high", status="muted")
        world.finding(a, 45, "medium")
        body = await world.get(days=7)
        days = body["findings"]["days"]
        assert [d["day"] for d in days] == [(TODAY - timedelta(days=n)).isoformat() for n in range(6, -1, -1)]
        assert days[-1] == {"day": TODAY.isoformat(), "critical": 1, "high": 0, "medium": 0, "low": 1}
        assert days[-3] == {"day": day(2).date().isoformat(), "critical": 0, "high": 2, "medium": 0, "low": 0}
        assert all(not (d["critical"] or d["high"] or d["medium"] or d["low"]) for d in days[:4] + [days[5]])
        assert body["findings"]["open"] == {"critical": 1, "high": 1, "medium": 1, "low": 0}
        # The inbox counts the same unresolved findings.
        inbox = (await c.get("/api/findings", params={"status": "open"})).json()
        assert len(inbox["items"]) == sum(body["findings"]["open"].values())


async def test_filters_by_asset_and_source(admin_env):
    """An asset prefix and a source narrow series, tiles, worst and findings; a filter that
    matches nothing gives the same shape with zeros."""
    async with admin_env.client() as c:
        world = World(admin_env, c)
        feeder, mill = (
            await world.series("feeder", "plant/feeder-4"),
            await world.series("mill", "mill/line_1"),
        )
        world.score(feeder, 0, 80.0, completeness=70.0)
        world.score(mill, 0, 40.0, completeness=30.0)
        world.finding(feeder, 0, "high")
        world.finding(mill, 0, "critical")
        upload_source = admin_env.scalar("SELECT source_id FROM series WHERE id = :s", s=feeder)
        other = (await c.post("/api/sources", json={"type": "synthetic", "name": "Other"})).json()["id"]

        plant = await world.get(asset="plant/")
        assert plant["series"] == {"total": 1, "scored": 1}
        assert tile(plant, "overall")["mean"] == 80.0 and tile(plant, "overall")["trend"][0]["n"] == 1
        assert [i["name"] for i in plant["worst"]["items"]] == ["feeder"]
        assert plant["findings"]["open"] == {"critical": 0, "high": 1, "medium": 0, "low": 0}
        assert plant["findings"]["days"][-1]["high"] == 1 and plant["findings"]["days"][-1]["critical"] == 0
        # `_` and `%` in a prefix are characters, not wildcards.
        assert (await world.get(asset="mill/line_"))["series"]["total"] == 1
        assert (await world.get(asset="mill/line%"))["series"]["total"] == 0

        mine = await world.get(source=str(upload_source))
        assert mine["series"] == {"total": 2, "scored": 2} and tile(mine, "overall")["mean"] == 60.0
        empty = await world.get(source=other)
        assert empty["series"] == {"total": 0, "scored": 0}
        assert all(t["mean"] is None and t["trend"] == [] for t in empty["tiles"])
        assert empty["worst"] == {"share": 0.05, "n": 0, "items": []}
        assert sum(empty["findings"]["open"].values()) == 0 and len(empty["findings"]["days"]) == 30
        # The sources are always listed in full.
        assert {s["name"] for s in empty["sources"]} == {s["name"] for s in mine["sources"]}
        assert len(empty["sources"]) == 2


async def test_sources_strip(admin_env):
    """Every source with its health, whether it is enabled and how many series it has."""
    async with admin_env.client() as c:
        world = World(admin_env, c)
        await world.series("a")
        await world.series("b")
        created = (await c.post("/api/sources", json={"type": "synthetic", "name": "Historian"})).json()
        admin_env.sql(
            "UPDATE sources SET enabled = false, health = CAST(:h AS jsonb) WHERE id = :i",
            h=json.dumps({"status": "failing", "last_fetch": LAST_FETCH}),
            i=created["id"],
        )
        sources = (await world.get())["sources"]
        by = {s["name"]: s for s in sources}
        assert [s["name"] for s in sources] == sorted(by, key=str.lower)
        assert by["Historian"] == {
            "id": created["id"],
            "name": "Historian",
            "type": "synthetic",
            "enabled": False,
            "status": "failing",
            "last_fetch": LAST_FETCH,
            "n_series": 0,
        }
        upload = next(s for s in sources if s["name"] != "Historian")
        assert (upload["n_series"], upload["enabled"]) == (2, True)


async def test_validation_and_roles(admin_env):
    """Only 7, 30 and 90 days; a source must be an id; viewers read, no role is 404."""
    async with admin_env.client() as c:
        for params, field in (
            ({"days": 31}, "days"),
            ({"days": 0}, "days"),
            ({"source": "nope"}, "source"),
            ({"asset": "x" * 201}, "asset"),
        ):
            r = await c.get("/api/overview", params=params)
            assert r.status_code == 422 and r.json()["detail"][0]["field"] == field, r.text
        assert (await c.get("/api/overview", params={"days": "many"})).status_code == 422
        assert (await c.get("/api/overview", params={"asset": "  ", "unknown": 1})).status_code == 200
    async with admin_env.client(MIA) as viewer:
        assert (await viewer.get("/api/overview")).status_code == 200
    async with admin_env.client(NED) as stranger:
        assert (await stranger.get("/api/overview")).status_code == 404


async def test_other_workspace_and_org_see_nothing(admin_env):
    """Another org sees none of this workspace's numbers, also when it names its source."""
    async with admin_env.client() as c:
        world = World(admin_env, c)
        a = await world.series("a", "plant/a")
        world.score(a, 0, 50.0, completeness=40.0)
        world.finding(a, 0, "critical")
        source = admin_env.scalar("SELECT source_id FROM series WHERE id = :s", s=a)
        assert (await world.get())["series"]["scored"] == 1
    async with admin_env.client(BOB, org=ORG_B) as other:
        for params in ({}, {"source": str(source)}, {"asset": "plant/"}):
            body = (await other.get("/api/overview", params=params)).json()
            assert body["series"] == {"total": 0, "scored": 0}
            assert all(t["n_series"] == 0 and t["trend"] == [] for t in body["tiles"])
            assert body["worst"]["items"] == [] and sum(body["findings"]["open"].values()) == 0
            assert all(d["critical"] == 0 for d in body["findings"]["days"])
            assert all(s["id"] != str(source) for s in body["sources"])


async def test_ten_thousand_series_under_a_second(admin_env):
    """10 000 series with 30 days of scores (300 000 rows) and 50 000 findings: the overview
    answers in under a second."""
    async with admin_env.client() as c:
        world = World(admin_env, c)
        seed = await world.series("seed")
        source = admin_env.scalar("SELECT source_id FROM series WHERE id = :s", s=seed)
        ids = {"o": DEFAULT_ORG_ID, "w": DEFAULT_WORKSPACE_ID}
        admin_env.sql(
            "INSERT INTO series (id, org_id, workspace_id, source_id, external_id, name, asset_path) "
            "SELECT gen_random_uuid(), :o, :w, :src, 'bulk-' || n, 'bulk ' || n, 'plant/unit-' || (n % 50) "
            "FROM generate_series(1, 9999) n",
            src=source,
            **ids,
        )
        admin_env.sql(
            "INSERT INTO score_days (org_id, workspace_id, series_id, layer, day, overall, completeness, "
            "timeliness, validity, accuracy, consistency, plausibility, integrity, n_findings, computed_at) "
            "SELECT :o, :w, s.id, 'raw', d::date, 60 + random() * 40, 50 + random() * 50, 100, 100, 100, "
            "100, 70 + random() * 30, 100, 0, d + interval '12 hours' "
            "FROM series s, generate_series(:first, :last, interval '1 day') d WHERE s.workspace_id = :w",
            first=day(29).replace(hour=0),
            last=NOON.replace(hour=0),
            **ids,
        )
        admin_env.sql(
            "INSERT INTO findings (org_id, workspace_id, id, series_id, check_id, dimension, severity, "
            "window_start, window_end, summary, status, created_at) "
            "SELECT :o, :w, gen_random_uuid(), s.id, 'tby.flatline', 'plausibility', "
            "(ARRAY['low', 'medium', 'high', 'critical'])[1 + (g % 4)], "
            ":t - age.back - interval '1 hour', :t - age.back, 'x', "
            "(ARRAY['open', 'acked', 'resolved'])[1 + (g % 3)], :t - age.back "
            "FROM (SELECT id FROM series WHERE workspace_id = :w) s, generate_series(1, 5) g, "
            "LATERAL (SELECT make_interval(days => (g * 7) % 30) AS back) age",
            t=NOON,
            **ids,
        )
        admin_env.sql("ANALYZE series")
        admin_env.sql("ANALYZE score_days")
        admin_env.sql("ANALYZE findings")
        assert admin_env.scalar("SELECT count(*) FROM score_days") == 300_000
        assert admin_env.scalar("SELECT count(*) FROM findings") == 50_000

        timings = []
        for _ in range(3):
            started = time.perf_counter()
            r = await c.get("/api/overview")
            timings.append(time.perf_counter() - started)
            assert r.status_code == 200
        body = r.json()
        assert body["series"] == {"total": 10_000, "scored": 10_000}
        assert (body["worst"]["n"], len(body["worst"]["items"])) == (500, 50)
        assert len(tile(body, "overall")["trend"]) == 30 and tile(body, "overall")["trend"][0]["n"] == 10_000
        assert (
            sum(sum(d[s] for s in ("critical", "high", "medium", "low")) for d in body["findings"]["days"])
            == 50_000
        )
        assert min(timings) < 1.0, timings
        # Narrowed to one asset prefix it stays fast.
        started = time.perf_counter()
        narrowed = (await c.get("/api/overview", params={"asset": "plant/unit-7"})).json()
        assert time.perf_counter() - started < 1.0 and narrowed["series"]["total"] > 0
