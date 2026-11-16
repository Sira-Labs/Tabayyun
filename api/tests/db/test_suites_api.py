"""Check suites API (spec 027): CRUD, validation, thresholds, roles and audit, run now, history."""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, timedelta

from admin_support import ALICE, MIA

HOUR0 = datetime(2026, 1, 1, tzinfo=UTC)
WINDOW = {"start": HOUR0.isoformat(), "end": (HOUR0 + timedelta(hours=96)).isoformat()}


def _csv(hours: int = 72) -> bytes:
    rows = ["ts,value"]
    for h in range(hours):
        value = 50 + 10 * math.sin(h / 24 * 2 * math.pi)
        rows.append(f"{(HOUR0 + timedelta(hours=h)).isoformat()},{value:.3f}")
    return ("\n".join(rows) + "\n").encode()


async def _dataset(client, name: str = "PT") -> str:
    """A dataset of one cached series whose window ends a day after its data."""
    r = await client.post(
        "/api/runs", files={"file": ("f.csv", _csv(), "text/csv")}, data={"series_id": f"{name}-a"}
    )
    assert r.status_code == 202, r.text
    run = (await client.get(f"/api/runs/{r.json()['id']}")).json()
    assert run["status"] == "succeeded", run
    body = {"name": name, "series_ids": [run["series"][0]["id"]], "window": WINDOW}
    r = await client.post("/api/datasets", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _suite(client, dataset_id: str, **fields) -> dict:
    r = await client.post("/api/suites", json={"name": "nightly", "dataset_id": dataset_id} | fields)
    assert r.status_code == 201, r.text
    return r.json()


def _field(response) -> str:
    """The body field a 422 names."""
    [error] = response.json()["detail"]
    assert error["loc"][0] == "body", error
    return error["loc"][1]


async def test_crud_and_defaults(admin_env, tmp_path):
    async with admin_env.client(ALICE, cache_url=str(tmp_path)) as alice:
        catalogue = (await alice.get("/api/checks/catalogue")).json()
        assert len(catalogue) == 24
        for check in catalogue:
            assert check["scope"] in ("series", "group")
            assert {p for p, v in check["defaults"].items() if v is None} == set(check["auto"]), check["id"]

        dataset = await _dataset(alice)
        suite = await _suite(alice, dataset, schedule=" 5  * * * * ")
        # Without `checks`: every built-in check, enabled, with no overrides.
        assert [c["id"] for c in suite["checks"]] == [c["id"] for c in catalogue]
        assert all(c["enabled"] and c["params"] == {} for c in suite["checks"])
        assert (suite["schedule"], suite["enabled"]) == ("5 * * * *", True)
        assert (suite["dataset_id"], suite["dataset_name"]) == (dataset, "PT")
        assert suite["last_run"] is None and suite["last_run_at"] is None
        next_run = datetime.fromisoformat(suite["next_run_at"])
        assert next_run.minute == 5 and timedelta(0) < next_run - datetime.now(UTC) <= timedelta(hours=1)

        assert (await alice.get(f"/api/suites/{suite['id']}")).json() == suite
        other = await _suite(alice, dataset, name="weekly", schedule=None)
        assert other["next_run_at"] is None
        listed = (await alice.get("/api/suites")).json()
        assert [s["name"] for s in listed["items"]] == ["weekly", "nightly"] and listed["next_cursor"] is None
        page = (await alice.get("/api/suites", params={"limit": 1})).json()
        assert [s["name"] for s in page["items"]] == ["weekly"] and page["next_cursor"]
        rest = (await alice.get("/api/suites", params={"limit": 1, "cursor": page["next_cursor"]})).json()
        assert [s["name"] for s in rest["items"]] == ["nightly"] and rest["next_cursor"] is None

        # Disabling clears the next run; a new schedule on an enabled suite sets it.
        r = await alice.patch(f"/api/suites/{suite['id']}", json={"enabled": False, "name": "hourly"})
        assert r.status_code == 200, r.text
        assert (r.json()["name"], r.json()["enabled"], r.json()["next_run_at"]) == ("hourly", False, None)
        r = await alice.patch(f"/api/suites/{suite['id']}", json={"enabled": True, "schedule": "0 3 * * *"})
        assert datetime.fromisoformat(r.json()["next_run_at"]).hour == 3
        r = await alice.patch(f"/api/suites/{suite['id']}", json={"schedule": None})
        assert (r.json()["schedule"], r.json()["next_run_at"]) == (None, None)

        # A deleted dataset leaves the suite, without a dataset.
        assert (await alice.delete(f"/api/datasets/{dataset}")).status_code == 204
        orphan = (await alice.get(f"/api/suites/{suite['id']}")).json()
        assert (orphan["dataset_id"], orphan["dataset_name"]) == (None, None)

        assert (await alice.delete(f"/api/suites/{suite['id']}")).status_code == 204
        for missing in (suite["id"], str(uuid.uuid4()), "not-a-uuid"):
            assert (await alice.get(f"/api/suites/{missing}")).status_code == 404
        assert (await alice.delete(f"/api/suites/{suite['id']}")).status_code == 404


async def test_validation_422_409(admin_env, tmp_path):
    async with admin_env.client(ALICE, cache_url=str(tmp_path)) as alice:
        dataset = await _dataset(alice)
        base = {"name": "s", "dataset_id": dataset}
        flatline = {"id": "tby.flatline"}
        for change, field in [
            ({"name": "  "}, "name"),
            ({"name": "x" * 257}, "name"),
            ({"dataset_id": str(uuid.uuid4())}, "dataset_id"),
            ({"schedule": "* * * *"}, "schedule"),
            ({"schedule": "61 * * * *"}, "schedule"),
            ({"schedule": "@hourly"}, "schedule"),
            ({"schedule": "0 0 * * * *"}, "schedule"),  # six fields: seconds are not offered
            ({"schedule": "* * * * *"}, "schedule"),  # every minute
            ({"schedule": "*/4 * * * *"}, "schedule"),
            ({"schedule": "0,3 * * * *"}, "schedule"),  # three minutes apart once an hour
            # 20:58 and 21:01, after thirty well-spaced firings of the day.
            ({"schedule": "1,6,58 0-20/2,21 * * *"}, "schedule"),
            ({"schedule": "0 0 31 2 *"}, "schedule"),  # parses, but there is no 31 February
            ({"checks": []}, "checks"),
            ({"checks": [{"id": "tby.nope"}]}, "checks"),
            ({"checks": [flatline, flatline]}, "checks"),
            ({"checks": [flatline | {"params": {"no_such": 1}}]}, "checks"),
            ({"checks": [flatline | {"params": {"min_run": "many"}}]}, "checks"),
            ({"checks": [flatline | {"enabled": False}]}, "checks"),  # nothing would run
        ]:
            r = await alice.post("/api/suites", json=base | change)
            assert r.status_code == 422, (change, r.text)
            assert _field(r) == field, (change, r.text)
        # Unknown body fields are refused by the schema.
        assert (await alice.post("/api/suites", json=base | {"cron": "5 * * * *"})).status_code == 422
        assert (await alice.get("/api/suites")).json()["items"] == []

        for schedule in ("*/5 * * * *", "0 6 * * 1-5", "30 2 1 * *", "58 23 * * *", "0,55 */2 * * *"):
            r = await alice.post("/api/suites", json=base | {"name": schedule, "schedule": schedule})
            assert r.status_code == 201, r.text
        suite = await _suite(alice, dataset, name="s")
        assert (await alice.post("/api/suites", json=base)).status_code == 409
        other = await _suite(alice, dataset, name="t")
        assert (await alice.patch(f"/api/suites/{other['id']}", json={"name": " s "})).status_code == 409
        # Its own name is no clash.
        r = await alice.patch(f"/api/suites/{suite['id']}", json={"name": "s"})
        assert r.status_code == 200, r.text
        suite = r.json()
        for change, field in [
            ({"name": None}, "name"),
            ({"enabled": None}, "enabled"),
            ({"checks": None}, "checks"),
            ({"dataset_id": None}, "dataset_id"),
            ({"schedule": "*/2 * * * *"}, "schedule"),
            ({"checks": [{"id": "tby.spikes", "params": {"t": "high"}}]}, "checks"),
        ]:
            r = await alice.patch(f"/api/suites/{suite['id']}", json=change)
            assert r.status_code == 422 and _field(r) == field, (change, r.text)
        assert (await alice.get(f"/api/suites/{suite['id']}")).json()["updated_at"] == suite["updated_at"]


async def test_thresholds_name_their_source(admin_env, tmp_path):
    async with admin_env.client(ALICE, cache_url=str(tmp_path)) as alice:
        checks = [
            {"id": "tby.flatline", "params": {"min_run": 20}},
            {"id": "tby.spikes", "enabled": False},
            {"id": "tby.physical_range", "params": {"max": 120.0}},
        ]
        suite = await _suite(alice, await _dataset(alice), checks=checks)
        by_id = {c["id"]: c for c in suite["checks"]}
        assert [c["id"] for c in suite["checks"]] == [c["id"] for c in checks]
        assert [(c["enabled"], c["scope"]) for c in suite["checks"]] == [
            (True, "series"),
            (False, "series"),
            (True, "series"),
        ]

        def threshold(check_id: str, param: str) -> tuple:
            [t] = [t for t in by_id[check_id]["thresholds"] if t["param"] == param]
            return t["value"], t["source"], bool(t["note"])

        assert threshold("tby.flatline", "min_run") == (20, "override", False)
        assert threshold("tby.flatline", "frozen_fraction") == (0.99, "default", False)
        assert threshold("tby.flatline", "atol") == (None, "metadata", True)
        assert threshold("tby.spikes", "min_scale") == (None, "baseline", True)
        assert threshold("tby.physical_range", "max") == (120.0, "override", False)
        assert threshold("tby.physical_range", "min") == (None, "metadata", True)
        # An explicit null for a run-time parameter is still decided at run time.
        r = await alice.patch(
            f"/api/suites/{suite['id']}",
            json={"checks": [{"id": "tby.physical_range", "params": {"max": None, "max_findings": 5}}]},
        )
        assert r.status_code == 200, r.text
        by_id = {c["id"]: c for c in r.json()["checks"]}
        assert threshold("tby.physical_range", "max") == (None, "metadata", True)
        assert threshold("tby.physical_range", "max_findings") == (5, "override", False)
        catalogue = {c["id"]: c for c in (await alice.get("/api/checks/catalogue")).json()}
        for check in suite["checks"]:
            assert [t["param"] for t in check["thresholds"]] == list(catalogue[check["id"]]["defaults"])


async def test_roles_and_audit(admin_env, tmp_path):
    settings = {"cache_url": str(tmp_path)}
    async with admin_env.client(ALICE, **settings) as alice:
        dataset = await _dataset(alice)
        suite = await _suite(alice, dataset, schedule="5 * * * *")
        sid = suite["id"]
        await alice.patch(f"/api/suites/{sid}", json={"schedule": "5 * * * *"})  # no change: no event
        changes = {"name": "renamed", "schedule": "10 * * * *", "checks": [{"id": "tby.flatline"}]}
        await alice.patch(f"/api/suites/{sid}", json=changes)
        run = await alice.post(f"/api/suites/{sid}/runs")
        assert run.status_code == 202, run.text
    async with admin_env.client(MIA, **settings) as mia:
        assert (await mia.get(f"/api/suites/{sid}")).json()["name"] == "renamed"
        assert [s["id"] for s in (await mia.get("/api/suites")).json()["items"]] == [sid]
        assert len((await mia.get("/api/checks/catalogue")).json()) == 24
        body = {"name": "mine", "dataset_id": dataset}
        assert (await mia.post("/api/suites", json=body)).status_code == 403
        assert (await mia.patch(f"/api/suites/{sid}", json={"enabled": False})).status_code == 403
        assert (await mia.post(f"/api/suites/{sid}/runs")).status_code == 403
        assert (await mia.delete(f"/api/suites/{sid}")).status_code == 403
    async with admin_env.client(ALICE, **settings) as alice:
        assert (await alice.delete(f"/api/suites/{sid}")).status_code == 204

    [created] = admin_env.events("suite.created")
    assert created.details == {"name": "nightly", "schedule": "5 * * * *"} and str(created.target_id) == sid
    [updated] = admin_env.events("suite.updated")
    assert updated.details == {
        "name": ["nightly", "renamed"],
        "schedule": ["5 * * * *", "10 * * * *"],
        "checks": ["changed", "changed"],
    }
    [started] = admin_env.events("suite.run_started")
    assert started.details == {"run_id": run.json()["id"]}
    [deleted] = admin_env.events("suite.deleted")
    assert deleted.details == {"name": "renamed"}


async def test_run_now_runs_only_the_suites_checks(admin_env, tmp_path):
    async with admin_env.client(ALICE, cache_url=str(tmp_path)) as alice:
        dataset = await _dataset(alice)
        # The data ends a day before the window: completeness finds that, and so would
        # staleness. A loose completeness threshold shows the suite's parameter reaches the core.
        checks = [
            {"id": "tby.completeness", "params": {"min_completeness": 0.5, "max_gap_findings": 0}},
            {"id": "tby.staleness", "enabled": False},
        ]
        suite = await _suite(alice, dataset, checks=checks, enabled=False)
        r = await alice.post(f"/api/suites/{suite['id']}/runs")
        assert r.status_code == 202 and r.json()["status"] == "queued", r.text
        run = (await alice.get(f"/api/runs/{r.json()['id']}")).json()
        assert run["status"] == "succeeded", run
        assert (run["trigger"], run["suite_id"], run["dataset_id"]) == ("suite", suite["id"], dataset)
        assert run["stats"]["n_findings"] == 0, run["stats"]

        after = (await alice.get(f"/api/suites/{suite['id']}")).json()
        assert after["last_run"] == {"id": run["id"], "status": "succeeded", "created_at": run["created_at"]}
        assert after["last_run_at"] is not None and after["next_run_at"] is None  # disabled

        # The same suite with the default threshold: only completeness reports.
        await alice.patch(
            f"/api/suites/{suite['id']}", json={"checks": [{"id": "tby.completeness"}, checks[1]]}
        )
        r = await alice.post(f"/api/suites/{suite['id']}/runs")
        strict = (await alice.get(f"/api/runs/{r.json()['id']}")).json()
        assert strict["status"] == "succeeded" and strict["stats"]["n_findings"] > 0, strict
        found = (await alice.get("/api/findings", params={"status": "all"})).json()["items"]
        assert {f["check_id"] for f in found if f["last_run_id"] == strict["id"]} == {"tby.completeness"}

        # A manual dataset run has no suite and runs every check.
        r = await alice.post("/api/runs", json={"dataset_id": dataset})
        manual = (await alice.get(f"/api/runs/{r.json()['id']}")).json()
        assert (manual["trigger"], manual["suite_id"]) == ("manual", None)
        found = (await alice.get("/api/findings", params={"status": "all"})).json()["items"]
        assert len({f["check_id"] for f in found if f["last_run_id"] == manual["id"]}) > 1


async def test_run_now_without_dataset_409(admin_env, tmp_path):
    async with admin_env.client(ALICE, cache_url=str(tmp_path)) as alice:
        dataset = await _dataset(alice)
        suite = await _suite(alice, dataset)
        assert (await alice.delete(f"/api/datasets/{dataset}")).status_code == 204
        r = await alice.post(f"/api/suites/{suite['id']}/runs")
        assert (r.status_code, r.json()["detail"]) == (409, "no_dataset")
        assert (await alice.post(f"/api/suites/{uuid.uuid4()}/runs")).status_code == 404
        assert (await alice.get("/api/runs", params={"suite_id": suite["id"]})).json()["items"] == []
    assert admin_env.events("suite.run_started") == []


async def test_history_by_suite_id(admin_env, tmp_path):
    async with admin_env.client(ALICE, cache_url=str(tmp_path)) as alice:
        dataset = await _dataset(alice)
        one = await _suite(alice, dataset, name="one", checks=[{"id": "tby.completeness"}])
        two = await _suite(alice, dataset, name="two", checks=[{"id": "tby.flatline"}])
        first = (await alice.post(f"/api/suites/{one['id']}/runs")).json()["id"]
        second = (await alice.post(f"/api/suites/{one['id']}/runs")).json()["id"]
        other = (await alice.post(f"/api/suites/{two['id']}/runs")).json()["id"]

        history = (await alice.get("/api/runs", params={"suite_id": one["id"]})).json()
        assert [r["id"] for r in history["items"]] == [second, first] and history["next_cursor"] is None
        assert {r["suite_id"] for r in history["items"]} == {one["id"]}
        page = (await alice.get("/api/runs", params={"suite_id": one["id"], "limit": 1})).json()
        assert [r["id"] for r in page["items"]] == [second] and page["next_cursor"]
        rest = (
            await alice.get(
                "/api/runs", params={"suite_id": one["id"], "limit": 1, "cursor": page["next_cursor"]}
            )
        ).json()
        assert [r["id"] for r in rest["items"]] == [first] and rest["next_cursor"] is None
        assert [
            r["id"] for r in (await alice.get("/api/runs", params={"suite_id": two["id"]})).json()["items"]
        ] == [other]
        assert (await alice.get("/api/runs", params={"suite_id": "nope"})).status_code == 422
        assert len((await alice.get("/api/runs")).json()["items"]) == 4  # the upload and three suite runs

        # Deleting the suite keeps its runs, without a suite.
        assert (await alice.delete(f"/api/suites/{one['id']}")).status_code == 204
        assert (await alice.get(f"/api/runs/{first}")).json()["suite_id"] is None
