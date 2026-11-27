"""Corrections against the database and a cache folder (spec 035): propose, compute, approve
and reject, the corrected layer they make, and that raw is never written."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pytest

from admin_support import ADAM, BOB, MIA, NED, ORG_B
from tabayyun.db import for_org
from tabayyun.db.models import BOOTSTRAP_USER_ID, DEFAULT_ORG_ID, DEFAULT_WORKSPACE_ID, Series
from tabayyun.services import corrections
from tabayyun.services.timeconv import datetime_to_ns
from tenancy import org_session

HOUR0 = datetime(2026, 1, 1, tzinfo=UTC)
H0 = datetime_to_ns(HOUR0)
HOUR = 3600 * 10**9
HOURS = 48
MISSING = (10, 11, 12)
GOOD, BAD, ESTIMATED = 0, 2, 3
RAW = [(h, float(h), GOOD) for h in range(HOURS) if h not in MISSING]


def at(hour: int) -> str:
    """The ns of an hour of the test series, as the API takes it."""
    return str(H0 + hour * HOUR)


class Lab:
    """One series with hourly values `h` over two days, hours 10 to 12 missing, and clients
    that share one cache folder."""

    def __init__(self, env, tmp_path) -> None:
        self.env, self.cache_url = env, str(tmp_path)
        self.sid = ""

    def client(self, user: uuid.UUID | None = None, **settings):
        return self.env.client(user, cache_url=self.cache_url, **settings)

    @property
    def app(self):
        return self.env.apps[-1]

    async def upload(self, client, name: str = "flow") -> str:
        rows = "ts,value\n" + "".join(f"{(HOUR0 + timedelta(hours=h)).isoformat()},{v}\n" for h, v, _ in RAW)
        r = await client.post(
            "/api/runs", files={"file": ("f.csv", rows.encode(), "text/csv")}, data={"series_id": name}
        )
        assert r.status_code == 202, r.text
        run = (await client.get(f"/api/runs/{r.json()['id']}")).json()
        assert run["status"] == "succeeded", run
        return run["series"][0]["id"]

    async def propose(self, client, method: str, start: int, end: int, expect: str = "computed", **params):
        """Propose over hours `[start, end)`; with inline jobs the answer is already computed."""
        r = await client.post(
            f"/api/series/{self.sid}/corrections",
            json={"method": method, "params": params, "from": at(start), "to": at(end), "reason": "test"},
        )
        assert r.status_code == 201, r.text
        assert r.json()["status"] == expect, r.json()
        return r.json()

    async def layer(self, version: int | None | str) -> list[tuple[int, float | None, int]]:
        """The series' layer as `(hour, value, quality code)`: raw (None), a version or `latest`."""
        async with org_session(self.app) as session:
            series = await session.get(Series, uuid.UUID(self.sid))
            if version == "latest":
                version = await corrections.latest_approved(session, series.id)
            batch = await corrections.read_layer(
                self.app.state.run_cache, session, series, version, H0 - HOUR, H0 + (HOURS + 1) * HOUR
            )
        return [] if batch is None else rows_of(batch)

    async def raw_cache(self) -> pa.RecordBatch:
        """The raw layer's rows as the cache gives them, every column."""
        async with org_session(self.app) as session:
            series = await session.get(Series, uuid.UUID(self.sid))
        found = await asyncio.to_thread(
            self.app.state.run_cache.read_series,
            source_id=str(series.source_id),
            series_ids=[self.sid],
            start_ns=H0 - HOUR,
            end_ns=H0 + (HOURS + 1) * HOUR,
        )
        return found[self.sid]

    def coverage(self) -> list[tuple]:
        return [
            tuple(r)
            for r in self.env.sql(
                "SELECT series_id, layer, range_start, range_end, rows, written_at FROM coverage "
                "ORDER BY series_id, layer, range_start"
            )
        ]


def rows_of(batch: pa.RecordBatch) -> list[tuple[int, float | None, int]]:
    ts = batch.column("ts").cast(pa.int64()).to_pylist()
    values = [None if v is None or v != v else v for v in batch.column("value").to_pylist()]
    return list(zip([(t - H0) // HOUR for t in ts], values, batch.column("quality").to_pylist(), strict=True))


@pytest.fixture
async def lab(admin_env, tmp_path):
    made = Lab(admin_env, tmp_path)
    async with made.client() as owner:
        made.sid = await made.upload(owner)
    return made


async def test_propose_compute_approve(lab):
    """A proposal is computed as version 1, lists what it added, and its approval makes the
    corrected layer; without inline jobs the job is queued and the worker's call computes it."""
    async with lab.client() as c:
        made = await lab.propose(c, "impute.linear", 8, 16, max_gap_ns=str(6 * HOUR))
        assert (made["version"], made["base_version"], made["error"]) == (1, None, None)
        assert made["params"] == {"max_gap_ns": str(6 * HOUR)}
        assert made["window"] == {"from": at(8), "to": at(16)} and made["reason"] == "test"
        assert made["stats"] == {"rows": 8, "changed": 0, "added": 3, "removed": 0, "unrepaired": 0}
        assert made["proposed_by"]["id"] == str(BOOTSTRAP_USER_ID) and made["proposed_by"]["name"]
        assert made["decided_by"] is None and made["computed_at"] and "ranges" not in made
        assert lab.env.scalar("SELECT params->>'max_gap_ns' FROM corrections") == str(6 * HOUR)
        assert lab.env.scalar("SELECT jsonb_typeof(params->'max_gap_ns') FROM corrections") == "number"

        one = (await c.get(f"/api/corrections/{made['id']}")).json()
        assert one["ranges"] == [
            {"from": at(10), "to": str(H0 + 12 * HOUR + 1), "changed": 0, "added": 3, "removed": 0}
        ]
        assert {k: v for k, v in one.items() if k != "ranges"} == made
        listed = (await c.get(f"/api/series/{lab.sid}/corrections")).json()
        assert listed == {"items": [made], "next_cursor": None}
        # Computed is not yet part of the layer.
        assert await lab.layer("latest") == RAW

        approved = await c.post(f"/api/corrections/{made['id']}/approve", json={"note": " modem down "})
        assert approved.status_code == 200, approved.text
        body = approved.json()
        assert (body["status"], body["decision_note"]) == ("approved", "modem down")
        assert body["decided_by"]["id"] == str(BOOTSTRAP_USER_ID) and body["decided_at"] and body["ranges"]
        expected = sorted(RAW + [(h, float(h), ESTIMATED) for h in MISSING])
        assert await lab.layer(1) == expected and await lab.layer("latest") == expected
        assert await lab.layer(None) == RAW
        again = await c.post(f"/api/corrections/{made['id']}/approve")
        assert (again.status_code, again.json()) == (409, {"detail": "not_computed"})

    async with lab.client(inline_jobs=False) as c:
        queued = await lab.propose(c, "mask", 20, 22, expect="proposed")
        assert (queued["version"], queued["stats"], queued["computed_at"]) == (2, None, None)
        [job] = lab.env.sql("SELECT queue_name, task_name, args FROM procrastinate_jobs")
        assert (job.queue_name, job.task_name) == ("runs", "tabayyun.apply_repair")
        assert job.args == {"correction_id": queued["id"], "org_id": str(DEFAULT_ORG_ID)}
        early = await c.post(f"/api/corrections/{queued['id']}/approve")
        assert (early.status_code, early.json()) == (409, {"detail": "not_computed"})
        factory = for_org(lab.app.state.session_factory, DEFAULT_ORG_ID)
        for _ in range(2):  # the second call finds it computed and leaves it alone
            await corrections.compute(factory, uuid.UUID(queued["id"]), lab.app.state.run_cache, 1000)
        done = (await c.get(f"/api/corrections/{queued['id']}")).json()
        assert (done["status"], done["base_version"]) == ("computed", 1)
        assert done["stats"] == {"rows": 2, "changed": 2, "added": 0, "removed": 0, "unrepaired": 0}
        assert len(done["ranges"]) == 1


async def test_raw_is_untouched(lab):
    """Computing and approving corrections leaves the raw rows in the cache and the raw
    coverage rows exactly as they were, and records no coverage of its own."""
    rows_before, coverage_before = await lab.raw_cache(), lab.coverage()
    assert rows_before.num_rows == len(RAW) and len(coverage_before) == 1
    async with lab.client() as c:
        for method, window, params in (
            ("impute.linear", (8, 16), {}),
            ("mask", (0, 6), {}),
            ("transform.affine", (0, HOURS), {"scale": 3, "offset": 1}),
        ):
            made = await lab.propose(c, method, *window, **params)
            assert (await c.post(f"/api/corrections/{made['id']}/approve")).status_code == 200
    assert await lab.layer("latest") != RAW
    rows_after = await lab.raw_cache()
    assert rows_after.schema == rows_before.schema and rows_after.equals(rows_before)
    assert lab.coverage() == coverage_before
    assert {layer for _, layer, *_ in lab.coverage()} == {"raw"}
    assert await lab.layer(None) == RAW


async def test_second_correction_builds_on_the_first(lab):
    """The second correction reads the first approved one: it transforms the clamped values."""
    async with lab.client() as c:
        clamp = await lab.propose(c, "clamp", 0, HOURS, max=20)
        assert clamp["stats"]["changed"] == 27 and clamp["base_version"] is None
        # Not approved yet: a correction computed now still builds on raw.
        on_raw = await lab.propose(c, "transform.affine", 40, 42, scale=2)
        assert on_raw["base_version"] is None
        assert (await c.post(f"/api/corrections/{clamp['id']}/approve")).status_code == 200

        scaled = await lab.propose(c, "transform.affine", 15, 30, scale=2, offset=1)
        assert (scaled["version"], scaled["base_version"]) == (3, 1)
        assert scaled["stats"] == {"rows": 15, "changed": 15, "added": 0, "removed": 0, "unrepaired": 0}
        assert (await c.post(f"/api/corrections/{scaled['id']}/approve")).status_code == 200

    def clamped(h: int) -> tuple[int, float, int]:
        return (h, float(min(h, 20)), GOOD if h <= 20 else ESTIMATED)

    v1 = [clamped(h) for h, _, _ in RAW]
    v3 = [(h, 2 * v + 1, q) if 15 <= h < 30 else (h, v, q) for h, v, q in v1]
    assert await lab.layer(None) == RAW
    assert await lab.layer(1) == v1 and await lab.layer(2) == v1
    assert await lab.layer(3) == v3 and await lab.layer("latest") == v3
    assert dict((h, v) for h, v, _ in v3)[25] == 41.0  # 2 · min(25, 20) + 1, not 2 · 25 + 1


async def test_rejected_is_not_in_the_layer(lab):
    """A rejected version stays in the list and in the cache, and no layer contains it."""
    async with lab.client() as c:
        masked = await lab.propose(c, "mask", 0, 6)
        rejected = await c.post(f"/api/corrections/{masked['id']}/reject", json={"note": "wrong window"})
        assert rejected.status_code == 200, rejected.text
        assert (rejected.json()["status"], rejected.json()["decision_note"]) == ("rejected", "wrong window")
        for action, code in (("reject", "already_decided"), ("approve", "not_computed")):
            answer = await c.post(f"/api/corrections/{masked['id']}/{action}")
            assert (answer.status_code, answer.json()) == (409, {"detail": code})
        assert await lab.layer("latest") == RAW and await lab.layer(1) == RAW

        scaled = await lab.propose(c, "transform.affine", 0, 3, scale=10)
        assert (scaled["version"], scaled["base_version"]) == (2, None)
        assert (await c.post(f"/api/corrections/{scaled['id']}/approve")).status_code == 200
        latest = await lab.layer("latest")
        assert latest[:6] == [(0, 0.0, GOOD), (1, 10.0, GOOD), (2, 20.0, GOOD), *RAW[3:6]]
        listed = (await c.get(f"/api/series/{lab.sid}/corrections")).json()["items"]
        assert [(x["version"], x["status"]) for x in listed] == [(2, "approved"), (1, "rejected")]
        only = (await c.get(f"/api/series/{lab.sid}/corrections", params={"status": "rejected"})).json()
        assert [x["id"] for x in only["items"]] == [masked["id"]]
    # The rejected version's rows are still in the cache.
    async with org_session(lab.app) as session:
        series = await session.get(Series, uuid.UUID(lab.sid))
    stored = await corrections.read_composed(
        lab.app.state.run_cache, series, [corrections.Layered(1, H0, H0 + 6 * HOUR)], H0, H0 + 6 * HOUR
    )
    assert rows_of(stored) == [(h, None, BAD) for h in range(6)]


async def test_stale_approval(lab):
    """A correction whose window overlaps one approved after it was computed cannot be
    approved; one elsewhere can, and proposing it again builds on the approved ones."""
    async with lab.client() as c:
        first = await lab.propose(c, "transform.affine", 0, 5, offset=100)
        second = await lab.propose(c, "transform.affine", 3, 8, scale=2)
        apart = await lab.propose(c, "mask", 20, 22)
        proposed = await lab.propose(c, "mask", 30, 31)
        assert (await c.post(f"/api/corrections/{first['id']}/approve")).status_code == 200
        stale = await c.post(f"/api/corrections/{second['id']}/approve")
        assert (stale.status_code, stale.json()) == (409, {"detail": "stale"})
        assert (await c.get(f"/api/corrections/{second['id']}")).json()["status"] == "computed"
        assert (await c.post(f"/api/corrections/{apart['id']}/approve")).status_code == 200
        # Stale or not, it can be rejected; so can one that was approved elsewhere not.
        assert (await c.post(f"/api/corrections/{second['id']}/reject")).status_code == 200
        assert (await c.post(f"/api/corrections/{proposed['id']}/reject")).status_code == 200
        decided = await c.post(f"/api/corrections/{apart['id']}/reject")
        assert (decided.status_code, decided.json()) == (409, {"detail": "already_decided"})

        again = await lab.propose(c, "transform.affine", 3, 8, scale=2)
        assert (again["version"], again["base_version"]) == (5, 3)
        assert (await c.post(f"/api/corrections/{again['id']}/approve")).status_code == 200
    latest = dict((h, v) for h, v, _ in await lab.layer("latest"))
    assert [latest[h] for h in range(9)] == [100.0, 101.0, 102.0, 206.0, 208.0, 10.0, 12.0, 14.0, 8.0]
    assert latest[20] is None and latest[30] == 30.0


async def test_a_lower_version_under_an_approved_one_is_stale(lab):
    """A correction computed after a higher, overlapping version was approved has that one in
    its base, yet the layer would lay the higher one over it: approving it is refused."""
    async with lab.client() as c:
        low = await lab.propose(c, "transform.affine", 0, 5, offset=1)
        high = await lab.propose(c, "transform.affine", 3, 8, offset=10)
        elsewhere = await lab.propose(c, "transform.affine", 20, 22, offset=1)
        assert (await c.post(f"/api/corrections/{high['id']}/approve")).status_code == 200
        # As if the worker had reached the lower versions only now, after that approval.
        lab.env.sql(
            "UPDATE corrections SET computed_at = clock_timestamp() WHERE id IN (:low, :elsewhere)",
            low=low["id"],
            elsewhere=elsewhere["id"],
        )
        stale = await c.post(f"/api/corrections/{low['id']}/approve")
        assert (stale.status_code, stale.json()) == (409, {"detail": "stale"})
        # A lower version whose window does not touch the approved one is unaffected.
        assert (await c.post(f"/api/corrections/{elsewhere['id']}/approve")).status_code == 200
    latest = dict((h, v) for h, v, _ in await lab.layer("latest"))
    assert [latest[h] for h in (0, 3, 7, 20)] == [0.0, 13.0, 17.0, 21.0]


async def test_validation(lab):
    """A refused operation is 422 with the core's message; windows, reasons, findings, ids,
    filters and cursors are checked."""
    async with lab.client() as c:
        other = await lab.upload(c, "other")
        url = f"/api/series/{lab.sid}/corrections"
        good = {"method": "mask", "params": {}, "from": at(0), "to": at(2), "reason": "why"}

        async def refused(**changes) -> str:
            answer = await c.post(url, json={**good, **changes})
            assert answer.status_code == 422, (changes, answer.text)
            return str(answer.json()["detail"])

        assert "unknown variant `smooth`" in await refused(method="smooth")
        assert "clamp needs min or max" in await refused(method="clamp")
        assert "days" in await refused(method="impute.like_day", params={"days": 0})
        assert await refused(method="dedupe", params={"keep": "middle"})
        assert "max_gap_ns" in await refused(method="impute.linear", params={"max_gap_ns": "1h"})
        assert "method" in await refused(params={"method": "clamp"})
        assert "unknown field" in await refused(params={"unexpected": 1})
        assert "later than from" in await refused(**{"from": at(2), "to": at(2)})
        assert "from" in await refused(**{"from": "2026-01-01T00:00:00Z"})
        assert "to" in await refused(to=str(2**63))
        assert "reason" in await refused(reason="   ")
        assert "reason" in await refused(reason="x" * 2001)
        assert await refused(surprise=1)
        assert await refused(finding_id=str(uuid.uuid4())) == "finding_not_found"
        assert await refused(finding_id="nope") == "finding_not_found"

        findings = {}
        for series in (lab.sid, other):
            findings[series] = uuid.uuid4()
            lab.env.sql(
                "INSERT INTO findings (org_id, workspace_id, id, series_id, check_id, dimension, severity, "
                "window_start, window_end, summary) VALUES (:o, :w, :id, :s, 'tby.gap', 'completeness', "
                "'high', :a, :b, 'gap')",
                o=DEFAULT_ORG_ID,
                w=DEFAULT_WORKSPACE_ID,
                id=findings[series],
                s=series,
                a=HOUR0,
                b=HOUR0 + timedelta(hours=1),
            )
        assert await refused(finding_id=str(findings[other])) == "finding_not_found"
        assert lab.env.scalar("SELECT count(*) FROM corrections") == 0

        # Integers for ns, a reason of exactly 2000 characters and the series' own finding pass.
        body = {
            **good,
            "from": H0,
            "to": H0 + HOUR,
            "reason": " " + "x" * 2000,
            "finding_id": str(findings[lab.sid]),
        }
        made = await c.post(url, json=body)
        assert made.status_code == 201, made.text
        assert made.json()["finding_id"] == str(findings[lab.sid]) and len(made.json()["reason"]) == 2000
        assert made.json()["window"] == {"from": at(0), "to": at(1)}

        for missing in ("nope", uuid.uuid4()):
            assert (await c.get(f"/api/series/{missing}/corrections")).status_code == 404
            assert (await c.post(f"/api/series/{missing}/corrections", json=good)).status_code == 404
            assert (await c.get(f"/api/corrections/{missing}")).status_code == 404
            assert (await c.post(f"/api/corrections/{missing}/approve")).status_code == 404
            assert (await c.post(f"/api/corrections/{missing}/reject")).status_code == 404
        for params in ({"status": "done"}, {"cursor": "nope"}, {"limit": 0}, {"limit": 201}):
            assert (await c.get(url, params=params)).status_code == 422, params
        note = await c.post(f"/api/corrections/{made.json()['id']}/reject", json={"note": "x" * 2001})
        assert note.status_code == 422


async def test_window_too_large(lab):
    """More rows than `TABAYYUN_CORRECTION_MAX_ROWS` (by coverage, with the operation's margin)
    fail the correction; nothing is written and it cannot be approved."""
    async with lab.client(correction_max_rows=10) as c:
        failed = await lab.propose(c, "mask", 0, HOURS, expect="failed")
        assert (failed["error"], failed["stats"], failed["base_version"]) == ("window too large", None, None)
        assert failed["computed_at"] is not None
        answer = await c.post(f"/api/corrections/{failed['id']}/approve")
        assert (answer.status_code, answer.json()) == (409, {"detail": "not_computed"})
        answer = await c.post(f"/api/corrections/{failed['id']}/reject")
        assert (answer.status_code, answer.json()) == (409, {"detail": "already_decided"})
        # The window is small, what the operation reads around it is not.
        wide = await lab.propose(c, "impute.linear", 8, 16, expect="failed")
        assert wide["error"] == "window too large"
        small = await lab.propose(c, "impute.linear", 8, 16, max_gap_ns=HOUR)
        assert (small["version"], small["status"]) == (3, "computed")
        assert small["stats"]["added"] == 0 and small["stats"]["unrepaired"] == 3
    assert lab.env.scalar("SELECT count(*) FROM corrected_ranges") == 0
    assert await lab.layer("latest") == RAW


async def test_roles_and_tenancy(lab):
    """Viewers read; editors propose and decide; another workspace or org knows nothing."""
    good = {"method": "mask", "params": {}, "from": at(0), "to": at(2), "reason": "why"}
    url = f"/api/series/{lab.sid}/corrections"
    async with lab.client(ADAM) as admin:
        made = await admin.post(url, json=good)
        assert made.status_code == 201 and made.json()["proposed_by"] == {"id": str(ADAM), "name": "adam"}
        cid = made.json()["id"]
    decisions = (f"/api/corrections/{cid}/approve", f"/api/corrections/{cid}/reject")
    async with lab.client(MIA) as viewer:
        assert [x["id"] for x in (await viewer.get(url)).json()["items"]] == [cid]
        assert (await viewer.get(f"/api/corrections/{cid}")).json()["status"] == "computed"
        assert (await viewer.post(url, json=good)).status_code == 403
        for path in decisions:
            assert (await viewer.post(path)).status_code == 403, path
    async with lab.client(NED) as stranger:
        assert (await stranger.get(url)).status_code == 404
        assert (await stranger.get(f"/api/corrections/{cid}")).status_code == 404
        assert (await stranger.post(url, json=good)).status_code == 404
    async with lab.client(BOB, org=ORG_B) as other_org:
        assert (await other_org.get(url)).status_code == 404
        assert (await other_org.post(url, json=good)).status_code == 404
        assert (await other_org.get(f"/api/corrections/{cid}")).status_code == 404
        for path in decisions:
            assert (await other_org.post(path)).status_code == 404, path
    # Another workspace of the same org does not know the series or the correction either.
    second = uuid.uuid4()
    lab.env.sql(
        "INSERT INTO workspaces (id, org_id, name) VALUES (:id, :o, 'second')", id=second, o=DEFAULT_ORG_ID
    )
    async with lab.client(headers={"X-Tabayyun-Workspace": str(second)}) as elsewhere:
        assert (await elsewhere.get("/api/series")).json()["items"] == []
        assert (await elsewhere.get(url)).status_code == 404
        assert (await elsewhere.get(f"/api/corrections/{cid}")).status_code == 404
        for path in decisions:
            assert (await elsewhere.post(path)).status_code == 404, path
    assert lab.env.scalar("SELECT status FROM corrections WHERE id = :id", id=cid) == "computed"
    assert lab.env.scalar("SELECT count(*) FROM corrections") == 1
    async with lab.client(ADAM) as admin:
        approved = await admin.post(decisions[0])
        assert approved.status_code == 200 and approved.json()["decided_by"]["id"] == str(ADAM)


async def test_audit_events(lab):
    """Proposing, approving and rejecting each write one event with actor, series, version
    and method; a refused request writes none."""
    async with lab.client(ADAM) as c:
        first = await lab.propose(c, "mask", 0, 2)
        second = await lab.propose(c, "impute.linear", 8, 16)
        await c.post(f"/api/corrections/{first['id']}/approve")
        await c.post(f"/api/corrections/{second['id']}/reject")
        assert (await c.post(f"/api/corrections/{second['id']}/approve")).status_code == 409
        assert (
            await c.post(f"/api/series/{lab.sid}/corrections", json={"method": "nope"})
        ).status_code == 422
    events = [e for e in lab.env.events() if e.action.startswith("correction.")]
    assert [(e.action, e.target_id) for e in events] == [
        ("correction.proposed", first["id"]),
        ("correction.proposed", second["id"]),
        ("correction.approved", first["id"]),
        ("correction.rejected", second["id"]),
    ]
    assert all(
        (e.actor_user_id, e.target_type, e.workspace_id) == (ADAM, "correction", DEFAULT_WORKSPACE_ID)
        for e in events
    )
    assert events[0].details == {
        "series_id": lab.sid,
        "series": "flow",
        "version": 1,
        "method": "mask",
        "window": {"from": at(0), "to": at(2)},
    }
    assert [(e.details["version"], e.details["method"]) for e in events[1:]] == [
        (2, "impute.linear"),
        (1, "mask"),
        (2, "impute.linear"),
    ]


async def test_versions_are_allocated_once(lab):
    """Proposals made at the same time get the versions 1 to n, each once, per series; the
    list pages through them newest first."""
    async with lab.client() as c:
        other = await lab.upload(c, "other")
        answers = await asyncio.gather(*(lab.propose(c, "mask", h, h + 1) for h in range(6)))
        assert sorted(a["version"] for a in answers) == [1, 2, 3, 4, 5, 6]
        mine = lab.sid
        lab.sid = other
        assert (await lab.propose(c, "mask", 0, 1))["version"] == 1
        lab.sid = mine
        assert (await lab.propose(c, "mask", 6, 7))["version"] == 7

        seen, cursor = [], None
        for _ in range(4):
            params = {"limit": 3} | ({"cursor": cursor} if cursor else {})
            page = (await c.get(f"/api/series/{lab.sid}/corrections", params=params)).json()
            seen.append([x["version"] for x in page["items"]])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert seen == [[7, 6, 5], [4, 3, 2], [1]]
    versions = lab.env.sql("SELECT series_id, version FROM corrections")
    assert len(versions) == len(set(versions)) == 8
