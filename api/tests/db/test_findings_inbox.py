"""Findings inbox (spec 029): filters, groups, mutes with an end, assignment, feedback, bulk
changes, audit, CSV export and saved filters."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from admin_support import ADAM, ALICE, BOB, MIA, NED, ORG_B, WORKSPACE_B
from tabayyun.db.models import DEFAULT_ORG_ID, DEFAULT_WORKSPACE_ID
from tabayyun.services import triage
from tabayyun.services.timeconv import datetime_to_ns

T0 = datetime(2026, 5, 1, tzinfo=UTC)


class Inbox:
    """Series made through the API and findings written as the owner, so each test controls
    exactly which findings exist."""

    def __init__(self, env, client) -> None:
        self.env, self.client = env, client
        self.series: dict[str, str] = {}
        self.n = 0

    async def add_series(self, name: str, asset_path: str | None = None) -> str:
        rows = "ts,value\n" + "".join(f"2026-01-01T{h:02d}:00:00Z,{h}\n" for h in range(24))
        r = await self.client.post(
            "/api/runs", files={"file": ("f.csv", rows.encode(), "text/csv")}, data={"series_id": name}
        )
        run = (await self.client.get(f"/api/runs/{r.json()['id']}")).json()
        assert run["status"] == "succeeded", run
        sid = run["series"][0]["id"]
        if asset_path is not None:
            r = await self.client.patch(f"/api/series/{sid}", json={"asset_path": asset_path})
            assert r.status_code == 200, r.text
        self.env.sql("DELETE FROM findings WHERE series_id = :s", s=sid)
        self.series[name] = sid
        return sid

    def finding(self, name: str, check: str = "tby.spikes", severity: str = "high", **fields) -> str:
        """One finding, an hour after the previous one; `fields` override its columns."""
        self.n += 1
        start = T0 + timedelta(hours=self.n)
        row = {
            "id": str(uuid.uuid4()),
            "org": DEFAULT_ORG_ID,
            "ws": DEFAULT_WORKSPACE_ID,
            "series": self.series[name],
            "check": check,
            "severity": severity,
            "start": start,
            "end": start + timedelta(minutes=30),
            "summary": f"{check} on {name}",
            "status": "open",
            "muted_until": None,
        } | fields
        self.env.sql(
            "INSERT INTO findings (org_id, workspace_id, id, series_id, check_id, dimension, severity, "
            "window_start, window_end, summary, status, muted_until) VALUES (:org, :ws, :id, :series, "
            ":check, 'plausibility', :severity, :start, :end, :summary, :status, :muted_until)",
            **row,
        )
        return row["id"]

    async def listed(self, client=None, **params) -> list[dict]:
        r = await (client or self.client).get(
            "/api/findings", params={"status": "all", "limit": 500} | params
        )
        assert r.status_code == 200, r.text
        return r.json()["items"]

    async def ids(self, **params) -> list[str]:
        return [f["id"] for f in await self.listed(**params)]

    def row(self, finding_id: str):
        [row] = self.env.sql("SELECT * FROM findings WHERE id = :i", i=finding_id)
        return row


@pytest.fixture
async def inbox(admin_env, tmp_path):
    async with admin_env.client(ALICE, cache_url=str(tmp_path)) as alice:
        inbox = Inbox(admin_env, alice)
        await inbox.add_series("feeder-1", "plant/north/feeder-1")
        await inbox.add_series("feeder-2", "plant/north")
        await inbox.add_series("pump-1", "plant/south/pump%1")
        await inbox.add_series("spare")
        yield inbox


async def test_filters_and_response_fields(inbox):
    a = inbox.finding("feeder-1", "tby.spikes", "critical", summary="Spike of 100% at noon")
    b = inbox.finding("feeder-2", "tby.flatline", "high")
    c = inbox.finding("pump-1", "tby.spikes", "low", status="resolved")
    d = inbox.finding("spare", "tby.completeness", "medium", summary="gap on SPARE line")
    r = await inbox.client.patch(
        f"/api/findings/{a}", json={"assignee_id": str(ADAM), "feedback": "false_positive"}
    )
    assert r.status_code == 200, r.text
    await inbox.client.patch(f"/api/findings/{b}", json={"assignee_id": str(ALICE)})

    [first] = await inbox.listed(series_id=inbox.series["feeder-1"])
    assert (first["series_name"], first["asset_path"]) == ("feeder-1", "plant/north/feeder-1")
    assert first["assignee"] == {"id": str(ADAM), "display_name": "adam"}
    assert (first["feedback"], first["muted_until"], first["status"]) == ("false_positive", None, "open")
    [spare] = await inbox.listed(series_id=inbox.series["spare"])
    assert (spare["asset_path"], spare["assignee"], spare["feedback"]) == (None, None, None)
    assert (await inbox.client.get(f"/api/findings/{a}")).json() == first

    # Newest window first; each filter alone.
    assert await inbox.ids() == [d, c, b, a]
    assert await inbox.ids(assignee="me") == [b]  # Alice asks
    assert await inbox.ids(assignee=str(ADAM)) == [a]
    assert await inbox.ids(assignee="none") == [d, c]
    assert await inbox.ids(feedback="false_positive") == [a]
    assert await inbox.ids(feedback="none") == [d, c, b]
    assert await inbox.ids(q="spare") == [d]  # the series name, and the summary in another case
    assert await inbox.ids(q="100%") == [a]  # `%` is text, not a wildcard
    assert await inbox.ids(q="_") == []
    assert await inbox.ids(asset="plant/north") == [b, a]  # the path and everything below it
    assert await inbox.ids(asset="plant/north/") == [b, a]
    assert await inbox.ids(asset="plant/nor") == []  # not a path prefix
    assert await inbox.ids(asset="plant") == [c, b, a]
    assert await inbox.ids(asset_path="plant/north") == [b]
    assert await inbox.ids(asset_path="plant/south/pump%1") == [c]
    assert await inbox.ids(asset_path="") == [d]
    source = (await inbox.client.get(f"/api/series/{inbox.series['spare']}")).json()["source_id"]
    assert len(await inbox.ids(source_id=source)) == 4
    assert await inbox.ids(source_id=str(uuid.uuid4())) == []
    # Combined with the existing filters.
    assert await inbox.ids(asset="plant", severity="critical,high", status="open") == [b, a]
    assert await inbox.ids(check_id="tby.spikes", feedback="none") == [c]
    assert [f["id"] for f in (await inbox.client.get("/api/findings")).json()["items"]] == [d, b, a]
    for bad in ({"assignee": "nobody"}, {"feedback": "meh"}, {"q": "x" * 201}, {"source_id": "x"}):
        assert (await inbox.client.get("/api/findings", params=bad)).status_code == 422, bad


async def test_groups_match_the_list(inbox):
    for series, check, severity in [
        ("feeder-1", "tby.spikes", "critical"),
        ("feeder-1", "tby.spikes", "high"),
        ("feeder-1", "tby.flatline", "high"),
        ("feeder-2", "tby.spikes", "low"),
        ("spare", "tby.spikes", "low"),
    ]:
        inbox.finding(series, check, severity)
    newest_resolved = inbox.finding("pump-1", "tby.flatline", "critical", status="resolved")

    async def groups(by: str, **params) -> list[dict]:
        r = await inbox.client.get("/api/findings/groups", params={"by": by} | params)
        assert r.status_code == 200, r.text
        assert r.json()["truncated"] is False
        return r.json()["items"]

    # Default statuses, like the list: the resolved finding is not counted.
    by_check = await groups("check")
    assert [(g["key"], g["label"], g["count"]) for g in by_check] == [
        ("tby.spikes", "tby.spikes", 4),
        ("tby.flatline", "tby.flatline", 1),
    ]
    assert by_check[0]["severities"] == {"critical": 1, "high": 1, "low": 2}
    assert by_check[0]["latest"] == datetime_to_ns(T0 + timedelta(hours=5))
    by_series = await groups("series", status="all")
    assert [(g["label"], g["count"]) for g in by_series][:1] == [("feeder-1", 3)]
    assert {g["key"] for g in by_series} == set(inbox.series.values())
    by_asset = await groups("asset", status="all")
    assert {g["key"]: (g["label"], g["count"]) for g in by_asset} == {
        "plant/north/feeder-1": ("plant/north/feeder-1", 3),
        "plant/north": ("plant/north", 1),
        "plant/south/pump%1": ("plant/south/pump%1", 1),
        "": ("No asset", 1),
    }
    # A group's key lists exactly that group, for every grouping and under a filter.
    for by, param in (("check", "check_id"), ("series", "series_id"), ("asset", "asset_path")):
        for filters in ({}, {"status": "all"}, {"severity": "low,critical", "status": "all"}):
            found = await groups(by, **filters)
            total = 0
            for group in found:
                listed = await inbox.listed(**({"status": "open,acked"} | filters | {param: group["key"]}))
                assert len(listed) == group["count"], (by, filters, group)
                total += group["count"]
            assert total == len(await inbox.listed(**({"status": "open,acked"} | filters))), (by, filters)
    assert newest_resolved in await inbox.ids(check_id="tby.flatline")
    assert (await inbox.client.get("/api/findings/groups", params={"by": "day"})).status_code == 422
    assert (await inbox.client.get("/api/findings/groups")).status_code == 422


async def test_mute_until_rules_and_expiry(inbox):
    a, b, c = (inbox.finding("feeder-1") for _ in range(3))
    now = datetime.now(UTC)
    until = now + timedelta(days=3)

    async def patch(finding_id: str, **body):
        return await inbox.client.patch(f"/api/findings/{finding_id}", json=body)

    r = await patch(a, status="muted", reason="sensor swap", muted_until=until.isoformat())
    assert r.status_code == 200, r.text
    assert datetime.fromisoformat(r.json()["muted_until"]) == until and r.json()["status"] == "muted"
    assert (await patch(b, status="muted", reason="known")).json()["muted_until"] is None  # no end
    for body, field in [
        ({"status": "muted", "muted_until": until.isoformat()}, "reason"),
        ({"status": "muted", "reason": "  ", "muted_until": until.isoformat()}, "reason"),
        (
            {"status": "muted", "reason": "r", "muted_until": (now - timedelta(minutes=1)).isoformat()},
            "muted_until",
        ),
        (
            {"status": "muted", "reason": "r", "muted_until": (now + timedelta(days=367)).isoformat()},
            "muted_until",
        ),
        ({"status": "acked", "muted_until": until.isoformat()}, "muted_until"),
        ({"muted_until": until.isoformat()}, "change"),
        ({}, "change"),
        ({"reason": "only a reason"}, "change"),
    ]:
        r = await patch(c, **body)
        assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", field], (body, r.text)
    assert (
        await patch(c, status="muted", reason="r", until="2030-01-01")
    ).status_code == 422  # unknown field
    assert inbox.row(c).status == "open"

    # Not yet expired: nothing happens. Expired: open again, with the reason and no end.
    factory = inbox.env.apps[-1].state.session_factory
    assert await triage.reopen_expired_mutes(factory, until - timedelta(seconds=1)) == 0
    assert await triage.reopen_expired_mutes(factory, until) == 1
    row = inbox.row(a)
    assert (row.status, row.status_reason, row.muted_until, row.status_at) == (
        "open",
        "mute expired",
        None,
        until,
    )
    assert inbox.row(b).status == "muted"  # a mute without an end stays
    assert await triage.reopen_expired_mutes(factory, until + timedelta(days=400)) == 0

    # Reopening by hand clears the end; muting again sets a new one.
    assert (await patch(a, status="muted", reason="again", muted_until=until.isoformat())).status_code == 200
    assert (await patch(a, status="open")).json()["muted_until"] is None
    assert inbox.row(a).muted_until is None


async def test_assignment_rules(inbox):
    a = inbox.finding("feeder-1")

    async def assign(user) -> tuple[int, dict]:
        r = await inbox.client.patch(
            f"/api/findings/{a}", json={"assignee_id": None if user is None else str(user)}
        )
        return r.status_code, r.json()

    assignees = (await inbox.client.get("/api/findings/assignees")).json()["items"]
    # The org's owners and its admin, by name; Mia is a viewer, Ned has no grant, Bob is in
    # another org.
    assert [(a["display_name"], a["id"]) for a in assignees][:2] == [
        ("adam", str(ADAM)),
        ("alice", str(ALICE)),
    ]
    assert {a["id"] for a in assignees}.isdisjoint({str(MIA), str(NED), str(BOB)})
    assert [a["display_name"].casefold() for a in assignees] == sorted(
        a["display_name"].casefold() for a in assignees
    )
    status, body = await assign(ADAM)
    assert status == 200 and body["assignee"]["id"] == str(ADAM) and body["status"] == "open"
    for user in (MIA, NED, BOB, uuid.uuid4()):
        status, body = await assign(user)
        assert status == 422 and body["detail"][0]["loc"] == ["body", "assignee_id"], (user, body)
    assert str(inbox.row(a).assignee_id) == str(ADAM)
    status, body = await assign(None)
    assert status == 200 and body["assignee"] is None

    # An editor through a direct grant becomes assignable; a disabled user does not.
    inbox.env.sql(
        "UPDATE workspace_memberships SET role = 'editor' WHERE user_id = :u AND workspace_id = :w",
        u=MIA,
        w=DEFAULT_WORKSPACE_ID,
    )
    assert (await assign(MIA))[0] == 200
    inbox.env.sql("UPDATE users SET disabled_at = now() WHERE id = :u", u=ADAM)
    assert (await assign(ADAM))[0] == 422
    names = [u["display_name"] for u in (await inbox.client.get("/api/findings/assignees")).json()["items"]]
    assert "mia" in names and "adam" not in names and "alice" in names
    # Deleting the user unassigns.
    inbox.env.sql("DELETE FROM workspace_memberships WHERE user_id = :u", u=MIA)
    inbox.env.sql("DELETE FROM org_memberships WHERE user_id = :u", u=MIA)
    inbox.env.sql("DELETE FROM users WHERE id = :u", u=MIA)
    assert inbox.row(a).assignee_id is None


async def test_feedback_does_not_change_status(inbox):
    a = inbox.finding("feeder-1")

    async def patch(**body):
        return await inbox.client.patch(f"/api/findings/{a}", json=body)

    r = await patch(feedback="false_positive")
    assert r.status_code == 200 and (r.json()["feedback"], r.json()["status"]) == ("false_positive", "open")
    assert inbox.row(a).feedback_at is not None and inbox.row(a).status_at is None
    # The mark survives a resolve and a reopen, and can be given together with a status.
    assert (await patch(status="resolved", reason="not real")).json()["feedback"] == "false_positive"
    assert (await patch(status="open")).json()["feedback"] == "false_positive"
    r = await patch(feedback=None)
    assert r.status_code == 200 and r.json()["feedback"] is None and inbox.row(a).feedback_at is None
    r = await patch(status="acked", feedback="false_positive")
    assert (r.json()["status"], r.json()["feedback"]) == ("acked", "false_positive")
    r = await patch(feedback="great")
    assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", "feedback"]


async def test_bulk_changes_what_it_may(inbox):
    open_ = inbox.finding("feeder-1")
    acked = inbox.finding("feeder-1", status="acked")
    muted = inbox.finding("feeder-2", status="muted")
    resolved = inbox.finding("pump-1", status="resolved")
    gone = str(uuid.uuid4())
    other_org = str(uuid.uuid4())
    series_b = uuid.uuid4()
    inbox.env.sql(
        "INSERT INTO sources (org_id, workspace_id, id, type, name) VALUES (:o, :w, :i, 'upload', 'b')",
        o=ORG_B,
        w=WORKSPACE_B,
        i=series_b,
    )
    inbox.env.sql(
        "INSERT INTO series (org_id, workspace_id, id, source_id, external_id, name) "
        "VALUES (:o, :w, :i, :i, 'x', 'x')",
        o=ORG_B,
        w=WORKSPACE_B,
        i=series_b,
    )
    inbox.finding("feeder-1", id=other_org, org=ORG_B, ws=WORKSPACE_B, series=str(series_b))

    async def bulk(ids: list[str], **change):
        return await inbox.client.post("/api/findings/bulk", json={"ids": ids, "change": change})

    everything = [open_, acked, muted, resolved, gone, other_org, open_]  # a repeated id counts once
    r = await bulk(everything, status="resolved", reason="cleaned up")
    assert r.status_code == 200, r.text
    assert sorted(r.json()["updated"]) == sorted([open_, acked])
    failed = {f["id"]: (f["error"], f["status"]) for f in r.json()["failed"]}
    assert failed == {
        muted: ("transition", "muted"),
        resolved: ("transition", "resolved"),
        gone: ("not_found", None),
        other_org: ("not_found", None),
    }
    assert [inbox.row(i).status for i in (open_, acked, muted, resolved)] == ["resolved"] * 2 + [
        "muted",
        "resolved",
    ]
    assert inbox.row(open_).status_reason == "cleaned up" and inbox.row(other_org).status == "open"

    # Assignment and feedback apply whatever the status.
    r = await bulk([open_, muted], assignee_id=str(ADAM), feedback="false_positive")
    assert sorted(r.json()["updated"]) == sorted([open_, muted]) and r.json()["failed"] == []
    assert {str(inbox.row(i).assignee_id) for i in (open_, muted)} == {str(ADAM)}

    # A change that is wrong in itself refuses the whole request.
    for change, field in [
        ({"assignee_id": str(MIA)}, "assignee_id"),
        ({"status": "muted"}, "reason"),
        ({}, "change"),
    ]:
        r = await bulk([open_], **change)
        assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", field], (change, r.text)
    assert (await bulk([], status="open")).status_code == 422
    assert (await bulk([str(uuid.uuid4()) for _ in range(501)], status="open")).status_code == 422
    assert (await bulk(["nope"], status="open")).status_code == 422
    r = await bulk([str(uuid.uuid4()) for _ in range(500)], status="open")
    assert r.status_code == 200 and len(r.json()["failed"]) == 500


async def test_audit_and_roles(admin_env, tmp_path):
    settings = {"cache_url": str(tmp_path)}
    async with admin_env.client(ALICE, **settings) as alice:
        inbox = Inbox(admin_env, alice)
        await inbox.add_series("feeder-1")
        a, b = inbox.finding("feeder-1"), inbox.finding("feeder-1")
    until = (datetime.now(UTC) + timedelta(days=2)).replace(microsecond=0)
    async with admin_env.client(ADAM, **settings) as adam:
        r = await adam.patch(
            f"/api/findings/{a}", json={"status": "muted", "reason": "swap", "muted_until": until.isoformat()}
        )
        assert r.status_code == 200, r.text
        await adam.patch(f"/api/findings/{a}", json={"assignee_id": str(ALICE), "feedback": "false_positive"})
        # Nothing changes: no event.
        await adam.patch(f"/api/findings/{a}", json={"assignee_id": str(ALICE), "feedback": "false_positive"})
        r = await adam.post("/api/findings/bulk", json={"ids": [a, b], "change": {"status": "acked"}})
        assert r.json()["updated"] == [b]  # `a` is muted: no event for it
        assert (
            await adam.patch(f"/api/findings/{b}", json={"status": "muted", "reason": "x"})
        ).status_code == 409
    events = [
        (e.action, e.target_id, e.details, e.actor_user_id)
        for e in admin_env.events()
        if e.target_type == "finding"
    ]
    # The two events of one request share its timestamp, so their order is not fixed.
    assert [events[0], *sorted(events[1:3], key=lambda e: e[0]), events[3]] == [
        (
            "finding.status_changed",
            a,
            {"from": "open", "to": "muted", "reason": "swap", "muted_until": until.isoformat()},
            ADAM,
        ),
        ("finding.assigned", a, {"from": None, "to": str(ALICE)}, ADAM),
        ("finding.feedback", a, {"from": None, "to": "false_positive"}, ADAM),
        ("finding.status_changed", b, {"from": "open", "to": "acked", "reason": None}, ADAM),
    ]
    assert {e.workspace_id for e in admin_env.events() if e.target_type == "finding"} == {
        DEFAULT_WORKSPACE_ID
    }

    async with admin_env.client(MIA, **settings) as mia:
        assert [
            f["id"] for f in (await mia.get("/api/findings", params={"status": "all"})).json()["items"]
        ] == [b, a]
        assert (await mia.get("/api/findings/groups", params={"by": "check"})).status_code == 200
        assert (await mia.get("/api/findings/assignees")).status_code == 200
        assert (await mia.get("/api/findings/export")).status_code == 200
        assert (await mia.patch(f"/api/findings/{b}", json={"status": "resolved"})).status_code == 403
        assert (await mia.patch(f"/api/findings/{b}", json={"feedback": "false_positive"})).status_code == 403
        r = await mia.post("/api/findings/bulk", json={"ids": [b], "change": {"status": "resolved"}})
        assert r.status_code == 403
    assert len([e for e in admin_env.events() if e.target_type == "finding"]) == 4


async def test_export_csv_safe_and_truncated(inbox, monkeypatch):
    a = inbox.finding("feeder-1", "tby.spikes", "critical", summary='=HYPERLINK("http://x","click")')
    b = inbox.finding("feeder-2", "tby.flatline", "high", summary="flat, with a comma\nand a line break")
    inbox.finding("spare", "tby.spikes", "low", status="resolved", summary="-1 below zero")
    await inbox.client.patch(f"/api/findings/{b}", json={"assignee_id": str(ADAM)})

    async def export(**params) -> tuple[list[dict], dict]:
        r = await inbox.client.get("/api/findings/export", params=params)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/csv")
        assert r.headers["content-disposition"] == 'attachment; filename="findings.csv"'
        return list(csv.DictReader(io.StringIO(r.text))), r.headers

    rows, headers = await export(status="all")
    assert headers["x-tabayyun-truncated"] == "false" and list(rows[0]) == list(triage.EXPORT_COLUMNS)
    assert [r["summary"] for r in rows] == [
        "'-1 below zero",
        "flat, with a comma\nand a line break",
        '\'=HYPERLINK("http://x","click")',
    ]
    middle = rows[1]
    assert (middle["id"], middle["series"], middle["asset_path"], middle["assignee"]) == (
        b,
        "feeder-2",
        "plant/north",
        "adam",
    )
    assert middle["window_start"] == (T0 + timedelta(hours=2)).isoformat() and middle["status"] == "open"
    # The same filters as the list.
    rows, _ = await export(severity="critical")
    assert [r["id"] for r in rows] == [a]
    rows, _ = await export()  # default statuses
    assert len(rows) == 2

    monkeypatch.setattr(triage, "MAX_EXPORT_ROWS", 2)
    monkeypatch.setattr(triage, "EXPORT_PAGE", 1)
    body, truncated = await _export_direct(inbox, max_rows=2)
    assert truncated and len(list(csv.DictReader(io.StringIO(body)))) == 2
    body, truncated = await _export_direct(inbox, max_rows=3)
    assert not truncated and len(list(csv.DictReader(io.StringIO(body)))) == 3


async def _export_direct(inbox, *, max_rows: int) -> tuple[str, bool]:
    from tabayyun.authz.scope import Scope
    from tabayyun.db import for_org
    from tabayyun.services.findings import FindingFilter

    factory = for_org(inbox.env.apps[-1].state.session_factory, DEFAULT_ORG_ID)
    async with factory() as session:
        return await triage.export_csv(
            session,
            Scope(org_id=DEFAULT_ORG_ID, workspace_id=DEFAULT_WORKSPACE_ID),
            FindingFilter(statuses=None),
            max_rows=max_rows,
        )


async def test_saved_filters_are_personal(admin_env, monkeypatch):
    async with admin_env.client(ALICE) as alice, admin_env.client(MIA) as mia:
        r = await alice.post(
            "/api/finding-filters",
            json={"name": " Open, critical ", "query": "?status=open&severity=critical"},
        )
        assert r.status_code == 201, r.text
        saved = r.json()
        assert (saved["name"], saved["query"]) == ("Open, critical", "status=open&severity=critical")
        assert (
            await alice.post("/api/finding-filters", json={"name": "Open, critical", "query": ""})
        ).json()["detail"] == "name_taken"
        await alice.post("/api/finding-filters", json={"name": "all mine", "query": "assignee=me&status=all"})
        names = [f["name"] for f in (await alice.get("/api/finding-filters")).json()["items"]]
        assert names == ["all mine", "Open, critical"]
        for body, field in [
            ({"name": " ", "query": ""}, "name"),
            ({"name": "x" * 81, "query": ""}, "name"),
            ({"name": "long", "query": "q=" + "x" * 2000}, "query"),
        ]:
            r = await alice.post("/api/finding-filters", json=body)
            assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", field], body

        # A viewer saves her own, under the same name, and sees only hers.
        r = await mia.post("/api/finding-filters", json={"name": "Open, critical", "query": "status=open"})
        assert r.status_code == 201, r.text
        assert [f["id"] for f in (await mia.get("/api/finding-filters")).json()["items"]] == [r.json()["id"]]
        assert (await mia.delete(f"/api/finding-filters/{saved['id']}")).status_code == 404
        assert len((await alice.get("/api/finding-filters")).json()["items"]) == 2
        assert (await alice.delete(f"/api/finding-filters/{saved['id']}")).status_code == 204
        for missing in (saved["id"], str(uuid.uuid4()), "nope"):
            assert (await alice.delete(f"/api/finding-filters/{missing}")).status_code == 404

        monkeypatch.setattr(triage, "MAX_FILTERS", 2)
        assert (
            await alice.post("/api/finding-filters", json={"name": "two", "query": ""})
        ).status_code == 201
        r = await alice.post("/api/finding-filters", json={"name": "three", "query": ""})
        assert (r.status_code, r.json()["detail"]) == (409, "too_many")
    # Personal preferences are not audited.
    assert [e for e in admin_env.events() if "filter" in e.action] == []
