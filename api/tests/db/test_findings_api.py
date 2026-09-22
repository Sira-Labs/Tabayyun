"""Findings, metrics and scores API with inline jobs (spec 003)."""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from synth_csv import faulty_csv
from tabayyun.db.models import Finding
from tabayyun.main import create_app
from tabayyun.services.findings import TRANSITIONS
from tabayyun.settings import Settings

FAULTS = ["gap", "flatline", "nans", "negative"]
STATUSES = ["open", "acked", "muted", "resolved"]
HOUR0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def app(db_url, fresh_schema):
    """App on a fresh schema, executing jobs inline in the request's background task."""
    fresh_schema("auto")
    return create_app(Settings(env="test", database_url=db_url, inline_jobs=True))


@pytest.fixture
async def client(app):
    """HTTP client whose requests complete their background tasks before returning."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await app.state.engine.dispose()


def hourly_csv(first_hour: int, last_hour: int, missing: range) -> bytes:
    """Hourly sine series over `[first_hour, last_hour]` with the hours in `missing` absent."""
    rows = ["ts,value"]
    for h in range(first_hour, last_hour + 1):
        if h in missing:
            continue
        ts = HOUR0 + timedelta(hours=h)
        rows.append(f"{ts.isoformat()},{50 + 10 * math.sin(h / 24 * 2 * math.pi):.3f}")
    return ("\n".join(rows) + "\n").encode()


async def _post_run(client, csv: bytes, **form: str) -> dict:
    """Post an upload with inline jobs and return the finished run."""
    data = {"series_id": "demo", "unit": "m3/h", **form}
    r = await client.post("/api/runs", files={"file": ("f.csv", csv, "text/csv")}, data=data)
    assert r.status_code == 202, r.text
    run = (await client.get(f"/api/runs/{r.json()['id']}")).json()
    assert run["status"] == "succeeded", run
    return run


async def _all_findings(client, **params: str) -> list[dict]:
    """Every finding matching `params` in one page."""
    r = await client.get("/api/findings", params={"limit": 500, **params})
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def _set_status(app, finding_id: str, status: str) -> None:
    """Force a finding's status directly in the database."""
    async with app.state.session_factory() as session, session.begin():
        await session.execute(
            update(Finding).where(Finding.id == uuid.UUID(finding_id)).values(status=status)
        )


async def test_same_upload_twice_keeps_one_finding_per_problem(client):
    """Uploading the synthetic faulty series twice: same count, occurrences 2, last run the second."""
    csv = faulty_csv(FAULTS)
    first = await _post_run(client, csv, quality_col="quality", now_ns="1700200000000000000")
    once = await _all_findings(client)
    assert once and first["stats"]["n_findings_new"] == len(once)
    second = await _post_run(client, csv, quality_col="quality", now_ns="1700200000000000000")
    twice = await _all_findings(client)
    assert len(twice) == len(once)
    assert second["stats"]["n_findings_merged"] == second["stats"]["n_findings"]
    for f in twice:
        assert f["occurrences"] == 2
        assert (f["first_run_id"], f["last_run_id"]) == (first["id"], second["id"])


async def test_overlapping_gap_in_a_second_file_merges_into_the_union(client):
    """File 2's gap overlaps file 1's gap: one gap finding whose window is the union."""
    file1 = hourly_csv(0, 95, range(40, 50))
    file2 = hourly_csv(24, 119, range(45, 56))
    windows = []
    for csv in (file1, file2):
        report = (
            await client.post(
                "/api/checks/run", files={"file": ("f.csv", csv, "text/csv")}, data={"series_id": "demo"}
            )
        ).json()
        [gap] = [
            f
            for f in report["findings"]
            if f["check_id"] == "tby.completeness" and "gap_start" in f["evidence"]
        ]
        windows.append(gap["window"])
        await _post_run(client, csv)
    gaps = [
        f for f in await _all_findings(client, check_id="tby.completeness") if "gap_start" in f["evidence"]
    ]
    assert len(gaps) == 1
    [gap] = gaps
    assert gap["occurrences"] == 2
    assert gap["window"]["start"] == min(w["start"] for w in windows)
    assert gap["window"]["end"] >= max(w["end"] for w in windows)
    assert gap["window"]["end"] - max(w["end"] for w in windows) < 1000  # rounded up to the microsecond


async def test_resolved_then_redetected_is_a_new_finding(client, app):
    """A resolved finding stays resolved; the next run reports the problem as a new finding."""
    csv = hourly_csv(0, 95, range(40, 50))
    await _post_run(client, csv)
    [gap] = [f for f in await _all_findings(client) if "gap_start" in f["evidence"]]
    r = await client.patch(
        f"/api/findings/{gap['id']}", json={"status": "resolved", "reason": "sensor replaced"}
    )
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["status_reason"]) == ("resolved", "sensor replaced")
    assert r.json()["status_at"]
    await _post_run(client, csv)
    gaps = [f for f in await _all_findings(client, status="all") if "gap_start" in f["evidence"]]
    assert sorted(f["status"] for f in gaps) == ["open", "resolved"]
    new = next(f for f in gaps if f["status"] == "open")
    assert new["id"] != gap["id"] and new["occurrences"] == 1


async def test_list_defaults_to_open_and_acked(client, app):
    """Without `status` the list hides muted and resolved; `status=all` shows everything."""
    await _post_run(client, faulty_csv(FAULTS), quality_col="quality")
    items = await _all_findings(client)
    assert len(items) >= 4
    for finding, status in zip(items[:4], STATUSES, strict=True):
        await _set_status(app, finding["id"], status)
    shown = {f["id"] for f in await _all_findings(client)}
    assert items[0]["id"] in shown and items[1]["id"] in shown
    assert items[2]["id"] not in shown and items[3]["id"] not in shown
    assert len(await _all_findings(client, status="all")) == len(items)
    muted = await _all_findings(client, status="muted,resolved")
    assert {f["id"] for f in muted} == {items[2]["id"], items[3]["id"]}
    assert (await client.get("/api/findings", params={"status": "gone"})).status_code == 422


async def test_filters_and_cursor_pagination(client):
    """Pages are stable, disjoint and ordered by window start; filters narrow the list."""
    run = await _post_run(client, faulty_csv(FAULTS), quality_col="quality")
    everything = await _all_findings(client)
    pages, cursor = [], None
    while True:
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        body = (await client.get("/api/findings", params=params)).json()
        pages.extend(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert [f["id"] for f in pages] == [f["id"] for f in everything]
    assert len({f["id"] for f in pages}) == len(pages)
    starts = [f["window"]["start"] for f in pages]
    assert starts == sorted(starts, reverse=True)

    high = await _all_findings(client, severity="high")
    assert high and all(f["severity"] == "high" for f in high)
    both = await _all_findings(client, severity="high,medium")
    assert len(both) == len([f for f in everything if f["severity"] in ("high", "medium")])
    flat = await _all_findings(client, check_id="tby.flatline")
    assert flat and all(f["check_id"] == "tby.flatline" for f in flat)
    assert all(
        f["dimension"] == "completeness" for f in await _all_findings(client, dimension="completeness")
    )
    assert len(await _all_findings(client, run_id=run["id"])) == len(everything)
    assert await _all_findings(client, run_id=str(uuid.uuid4())) == []
    series_id = run["series"][0]["id"]
    assert len(await _all_findings(client, series_id=series_id)) == len(everything)

    # A range that only one short finding overlaps, once as epoch ns and once as RFC 3339.
    target = min(everything, key=lambda f: f["window"]["end"] - f["window"]["start"])
    since, until = target["window"]["start"], target["window"]["end"]
    in_range = await _all_findings(client, since=str(since), until=str(until))
    assert target["id"] in [f["id"] for f in in_range]
    assert all(f["window"]["end"] > since and f["window"]["start"] < until for f in in_range)
    iso = datetime.fromtimestamp(since / 1e9, tz=UTC).isoformat()
    assert target["id"] in [f["id"] for f in await _all_findings(client, since=iso)]

    for bad in (
        {"severity": "urgent"},
        {"cursor": "garbage"},
        {"since": "yesterday"},
        {"series_id": "x"},
        {"until": "9" * 30},
    ):
        assert (await client.get("/api/findings", params=bad)).status_code == 422, bad
    assert (await client.get("/api/findings", params={"limit": 501})).status_code == 422


@pytest.mark.parametrize(("current", "target"), [(a, b) for a in STATUSES for b in STATUSES])
async def test_status_transitions_table(client, app, current, target):
    """Allowed moves answer 200 with the new status; the others 409 with the current status."""
    await _post_run(client, hourly_csv(0, 95, range(40, 50)))
    finding = (await _all_findings(client))[0]
    await _set_status(app, finding["id"], current)
    body = {"status": target, "reason": "known outage" if target == "muted" else None}
    r = await client.patch(f"/api/findings/{finding['id']}", json=body)
    if target in TRANSITIONS[current]:
        assert r.status_code == 200, r.text
        assert r.json()["status"] == target
    else:
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["status"] == current


async def test_mute_requires_reason(client):
    """Muting without a reason is 422; with one it is stored; unknown ids are 404."""
    await _post_run(client, hourly_csv(0, 95, range(40, 50)))
    finding_id = (await _all_findings(client))[0]["id"]
    for body in (
        {"status": "muted"},
        {"status": "muted", "reason": "   "},
        {"status": "muted", "reason": "x" * 501},
    ):
        assert (await client.patch(f"/api/findings/{finding_id}", json=body)).status_code == 422, body
    r = await client.patch(
        f"/api/findings/{finding_id}", json={"status": "muted", "reason": "planned outage"}
    )
    assert r.status_code == 200 and r.json()["status_reason"] == "planned outage"
    assert (await client.get(f"/api/findings/{finding_id}")).json()["status"] == "muted"
    assert (await client.patch(f"/api/findings/{finding_id}", json={"status": "resolved"})).status_code == 409
    assert (await client.get(f"/api/findings/{uuid.uuid4()}")).status_code == 404
    assert (await client.patch("/api/findings/not-a-uuid", json={"status": "acked"})).status_code == 404


async def test_metrics_and_scores_endpoints(client, app):
    """Scores and metric points come back newest first with the run that wrote them."""
    csv = faulty_csv(FAULTS)
    first = await _post_run(client, csv, quality_col="quality", now_ns="1700200000000000000")
    second = await _post_run(client, csv, quality_col="quality", now_ns="1700200000000000000")
    series_id = second["series"][0]["id"]

    scores = (await client.get(f"/api/series/{series_id}/scores")).json()["items"]
    assert [s["run_id"] for s in scores] == [second["id"], first["id"]]
    assert scores[0]["layer"] == "raw" and scores[0]["overall"] == second["series"][0]["score"]
    assert (
        len((await client.get(f"/api/series/{series_id}/scores", params={"limit": 1})).json()["items"]) == 1
    )

    points = (await client.get(f"/api/series/{series_id}/metrics", params={"limit": 1000})).json()["items"]
    assert len(points) == second["stats"]["n_metrics"]
    assert {p["run_id"] for p in points} == {second["id"]}
    stamps = [p["ts"] for p in points]
    assert stamps == sorted(stamps, reverse=True)
    named = (await client.get(f"/api/series/{series_id}/metrics", params={"name": "completeness"})).json()[
        "items"
    ]
    assert named and all(p["name"] == "completeness" for p in named)
    async with app.state.session_factory() as session:
        assert (await session.execute(select(Finding.series_id).limit(1))).scalar_one() == uuid.UUID(
            series_id
        )

    assert (await client.get(f"/api/series/{uuid.uuid4()}/scores")).status_code == 404
    assert (await client.get("/api/series/nope/metrics")).status_code == 404
