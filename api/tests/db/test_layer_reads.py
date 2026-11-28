"""Reading a layer through the chart, profile, compare, scatter and residual routes (spec
036): raw by default and unchanged, the corrected layer only when it is named."""

from __future__ import annotations

from datetime import timedelta

import pytest

from admin_support import BOB, MIA, NED, ORG_B
from db.test_corrections_api import HOUR, HOUR0, HOURS, MISSING, at, lab  # noqa: F401

WINDOW = {"from": at(0), "to": at(HOURS)}
N_RAW, N_FILLED = HOURS - len(MISSING), HOURS
BAD_LAYER = {"field": "layer", "message": "layer must be raw, corrected@latest or corrected@v{n}"}


async def approve(lab, client, method: str = "impute.linear", start: int = 8, end: int = 16, **params):  # noqa: F811
    """Propose (computed inline) and approve a correction; the approved correction."""
    made = await lab.propose(client, method, start, end, **params)
    r = await client.post(f"/api/corrections/{made['id']}/approve")
    assert r.status_code == 200, r.text
    return r.json()


async def upload_full(client, name: str) -> str:
    """A second series with every hour of the lab's two days."""
    rows = "ts,value\n" + "".join(f"{(HOUR0 + timedelta(hours=h)).isoformat()},{h}.0\n" for h in range(HOURS))
    r = await client.post(
        "/api/runs", files={"file": ("f.csv", rows.encode(), "text/csv")}, data={"series_id": name}
    )
    assert r.status_code == 202, r.text
    return (await client.get(f"/api/runs/{r.json()['id']}")).json()["series"][0]["id"]


async def pair(lab, client) -> tuple[str, str]:  # noqa: F811
    """A complete second series and a two-member group of it with the lab's series."""
    other = await upload_full(client, "other")
    body = {
        "name": "pair",
        "kind": "related",
        "members": [{"series_id": lab.sid, "role": "member"}, {"series_id": other, "role": "member"}],
        "params": {},
    }
    r = await client.post("/api/series-groups", json=body)
    assert r.status_code == 201, r.text
    return other, r.json()["id"]


def routes(lab, other: str, group: str) -> dict[str, tuple[str, dict]]:  # noqa: F811
    """The five reading routes over the lab's two days."""
    return {
        "chart": (f"/api/series/{lab.sid}/chart", dict(WINDOW)),
        "profile": (f"/api/series/{lab.sid}/profile", dict(WINDOW)),
        "compare": ("/api/compare/chart", {"series": [lab.sid, other], **WINDOW}),
        "scatter": ("/api/compare/scatter", {"x": lab.sid, "y": other, **WINDOW}),
        "residual": (f"/api/series-groups/{group}/residual", dict(WINDOW)),
    }


async def read_all(client, paths: dict[str, tuple[str, dict]], **extra) -> dict[str, tuple]:
    """Status, raw body bytes and ETag of each route."""
    out = {}
    for name, (path, params) in paths.items():
        r = await client.get(path, params={**params, **extra})
        out[name] = (r.status_code, r.content, r.headers.get("etag"))
    return out


async def test_chart_reads_a_layer(lab):  # noqa: F811
    """The chart shows the corrected rows when the layer is named, with the version read."""
    url = f"/api/series/{lab.sid}/chart"
    async with lab.client() as c:
        early = (await c.get(url, params={**WINDOW, "layer": "corrected@latest"})).json()
        # No approved correction: the corrected layer is the raw rows, and no version was read.
        assert (early["layer"], early["layer_version"], early["n_raw"]) == ("corrected@latest", None, N_RAW)
        await approve(lab, c, max_gap_ns=str(6 * HOUR))
        await approve(lab, c, "transform.affine", 0, 4, scale=1, offset=100)

        raw = (await c.get(url, params=WINDOW)).json()
        assert raw["layer"] == "raw" and "layer_version" not in raw and raw["n_raw"] == N_RAW
        assert at(10) not in raw["ts"] and raw["values"][raw["ts"].index(at(0))] == 0.0

        latest = (await c.get(url, params={**WINDOW, "layer": "corrected@latest"})).json()
        assert (latest["layer"], latest["layer_version"], latest["n_raw"]) == (
            "corrected@latest",
            2,
            N_FILLED,
        )
        assert latest["values"][latest["ts"].index(at(10))] == 10.0
        assert latest["values"][latest["ts"].index(at(0))] == 100.0
        assert {q["quality"] for q in latest["quality"]} == {"estimated"}
        # Windows and the extent stay the raw layer's coverage.
        assert latest["window"] == raw["window"] and latest["extent"] == raw["extent"]

        first = (await c.get(url, params={**WINDOW, "layer": "corrected@v1"})).json()
        assert (first["layer"], first["layer_version"], first["n_raw"]) == ("corrected@v1", 1, N_FILLED)
        assert first["values"][first["ts"].index(at(0))] == 0.0
        assert first["values"][first["ts"].index(at(10))] == 10.0
        # Without a window the default one is the raw layer's, whatever the layer.
        assert (await c.get(url, params={"layer": "corrected@v2"})).json()["window"] == (
            await c.get(url)
        ).json()["window"]

    async with lab.client(MIA) as viewer:
        seen = await viewer.get(url, params={**WINDOW, "layer": "corrected@latest"})
        assert seen.status_code == 200 and seen.json()["layer_version"] == 2
    async with lab.client(NED) as stranger:
        assert (await stranger.get(url, params={"layer": "corrected@latest"})).status_code == 404
    async with lab.client(BOB, org=ORG_B) as other_org:
        assert (await other_org.get(url, params={"layer": "corrected@latest"})).status_code == 404


async def test_profile_reads_a_layer(lab):  # noqa: F811
    """The profile of the corrected layer counts the estimated samples; raw has none."""
    url = f"/api/series/{lab.sid}/profile"
    async with lab.client() as c:
        await approve(lab, c, max_gap_ns=str(6 * HOUR))
        raw = (await c.get(url, params=WINDOW)).json()
        assert set(raw) == {"window", "profile", "quality_counts", "band"}
        assert raw["quality_counts"] == {"good": N_RAW, "uncertain": 0, "bad": 0, "estimated": 0}
        for name in ("corrected@latest", "corrected@v1"):
            body = (await c.get(url, params={**WINDOW, "layer": name})).json()
            assert (body["layer"], body["layer_version"]) == (name, 1)
            assert body["quality_counts"] == {"good": N_RAW, "uncertain": 0, "bad": 0, "estimated": 3}
            assert body["profile"] != raw["profile"] and body["window"] == raw["window"]
        assert (await c.get(url, params={**WINDOW, "layer": "raw"})).json() == raw


async def test_default_is_raw_and_unchanged(lab):  # noqa: F811
    """Without `layer` every route answers the same bytes and ETag before and after a
    correction is approved, with none of this spec's fields; `layer=raw` is the same answer."""
    async with lab.client() as c:
        other, group = await pair(lab, c)
        paths = routes(lab, other, group)
        before = await read_all(c, paths)
        assert all(status == 200 for status, _, _ in before.values()), before
        await approve(lab, c, max_gap_ns=str(6 * HOUR))
        await approve(lab, c, "transform.affine", 0, 4, scale=2)
        after = await read_all(c, paths)
        assert after == before
        assert await read_all(c, paths, layer="raw") == before
        assert before["chart"][2] and all(
            tag is None for name, (_, _, tag) in before.items() if name != "chart"
        )

        bodies = {name: (await c.get(path, params=params)).json() for name, (path, params) in paths.items()}
        assert set(bodies["chart"]) == {
            "window", "extent", "layer", "width_px", "n_raw", "ts", "values", "quality"
        }  # fmt: skip
        assert bodies["chart"]["layer"] == "raw"
        assert set(bodies["profile"]) == {"window", "profile", "quality_counts", "band"}
        assert set(bodies["compare"]) == {"bins", "series"}
        assert all("layer_version" not in s for s in bodies["compare"]["series"])
        assert "layer" not in bodies["scatter"] and set(bodies["scatter"]["x"]) == {"id", "name", "unit"}
        assert set(bodies["residual"]) == {"kind", "grid_ns", "window", "ts", "residual", "band", "missing"}
        # And they differ from the corrected answers, so the comparison above means something.
        named = await read_all(c, paths, layer="corrected@latest")
        assert all(named[name][1] != before[name][1] for name in paths)


async def test_etag_differs_by_layer(lab):  # noqa: F811
    """A cached raw chart is never taken for a corrected one, and an approval changes the tag
    of the layers it is part of."""
    url = f"/api/series/{lab.sid}/chart"

    async def tag(client, layer: str | None = None) -> str:
        r = await client.get(url, params={**WINDOW, **({"layer": layer} if layer else {})})
        assert r.status_code == 200, r.text
        return r.headers["etag"]

    async with lab.client() as c:
        raw, unlayered = await tag(c), await tag(c, "corrected@latest")
        assert raw != unlayered and await tag(c, "raw") == raw
        await approve(lab, c, max_gap_ns=str(6 * HOUR))
        latest, first = await tag(c, "corrected@latest"), await tag(c, "corrected@v1")
        assert len({raw, unlayered, latest, first}) == 4 and await tag(c) == raw
        # The raw tag does not validate a corrected answer; the layer's own tag does.
        stale = await c.get(
            url, params={**WINDOW, "layer": "corrected@latest"}, headers={"If-None-Match": raw}
        )
        assert stale.status_code == 200
        fresh = await c.get(
            url, params={**WINDOW, "layer": "corrected@latest"}, headers={"If-None-Match": latest}
        )
        assert fresh.status_code == 304 and fresh.headers["etag"] == latest
        assert (await c.get(url, params=WINDOW, headers={"If-None-Match": raw})).status_code == 304

        await approve(lab, c, "transform.affine", 0, 4, scale=2)
        assert await tag(c, "corrected@latest") != latest
        assert await tag(c, "corrected@v1") == first and await tag(c, "corrected@v2") != first
        assert await tag(c) == raw


async def test_unknown_version(lab):  # noqa: F811
    """`corrected@v{n}` must be an approved version of the series; any other name is refused
    on every route with the field it is about."""
    unknown = {"detail": "unknown_layer_version"}
    async with lab.client() as c:
        other, group = await pair(lab, c)
        paths = routes(lab, other, group)
        for name, (path, params) in paths.items():
            for bad in ("bogus", "corrected", "corrected@v0", "corrected@v01", "CORRECTED@LATEST", ""):
                r = await c.get(path, params={**params, "layer": bad})
                assert (r.status_code, r.json()) == (422, {"detail": [BAD_LAYER]}), (name, bad, r.text)

        computed = await lab.propose(c, "mask", 20, 22)
        rejected = await lab.propose(c, "mask", 30, 32)
        assert (await c.post(f"/api/corrections/{rejected['id']}/reject")).status_code == 200
        for route in ("chart", "profile"):
            path, params = paths[route]
            for version in (1, 2, 3, 99):  # computed, rejected, never proposed
                r = await c.get(path, params={**params, "layer": f"corrected@v{version}"})
                assert (r.status_code, r.json()) == (422, unknown), (route, version, r.text)
        assert (await c.post(f"/api/corrections/{computed['id']}/approve")).status_code == 200
        for route in ("chart", "profile"):
            path, params = paths[route]
            assert (await c.get(path, params={**params, "layer": "corrected@v1"})).json()[
                "layer_version"
            ] == 1
            r = await c.get(path, params={**params, "layer": "corrected@v2"})
            assert (r.status_code, r.json()) == (422, unknown)
        # A version of another series is not one of this series.
        r = await c.get(f"/api/series/{other}/chart", params={**WINDOW, "layer": "corrected@v1"})
        assert (r.status_code, r.json()) == (422, unknown)


async def test_compare_and_residual_read_corrected_latest(lab):  # noqa: F811
    """Compare chart, scatter and residual read each series' corrected layer: the filled hours
    of one series appear, the other series has no correction and reads raw."""
    latest = {"layer": "corrected@latest"}
    async with lab.client() as c:
        other, group = await pair(lab, c)
        paths = routes(lab, other, group)
        await approve(lab, c, max_gap_ns=str(6 * HOUR))

        path, params = paths["compare"]
        raw = (await c.get(path, params=params)).json()
        body = (await c.get(path, params={**params, **latest})).json()
        assert [s["n_raw"] for s in raw["series"]] == [N_RAW, N_FILLED]
        assert body["layer"] == "corrected@latest" and body["bins"] == raw["bins"]
        assert [(s["id"], s["n_raw"], s["layer_version"]) for s in body["series"]] == [
            (lab.sid, N_FILLED, 1),
            (other, N_FILLED, None),
        ]
        assert at(10) in body["series"][0]["ts"] and body["series"][1] == {
            **raw["series"][1],
            "layer_version": None,
        }

        path, params = paths["scatter"]
        raw = (await c.get(path, params=params)).json()
        body = (await c.get(path, params={**params, **latest})).json()
        assert (raw["n_bins"], body["n_bins"]) == (N_RAW, N_FILLED)
        assert body["layer"] == "corrected@latest"
        assert (body["x"]["layer_version"], body["y"]["layer_version"]) == (1, None)
        assert body["xs"] == body["ys"] and at(11) in body["ts"] and at(11) not in raw["ts"]

        path, params = paths["residual"]
        raw = (await c.get(path, params=params)).json()
        body = (await c.get(path, params={**params, **latest})).json()
        assert body["layer"] == "corrected@latest"
        assert body["layer_versions"] == {lab.sid: 1, other: None}
        assert at(11) in body["ts"] and at(11) not in raw["ts"]
        assert len([x for x in body["residual"] if x is not None]) == N_FILLED
        assert len([x for x in raw["residual"] if x is not None]) == N_RAW

    async with lab.client(MIA) as viewer:
        for path, params in paths.values():
            assert (await viewer.get(path, params={**params, **latest})).status_code == 200, path
    async with lab.client(BOB, org=ORG_B) as other_org:
        for path, params in paths.values():
            assert (await other_org.get(path, params={**params, **latest})).status_code == 404, path


@pytest.mark.parametrize("route", ["compare", "scatter", "residual"])
async def test_versions_refused_for_several_series(lab, route):  # noqa: F811
    """Versions count per series: the routes that read several take raw and corrected@latest."""
    async with lab.client() as c:
        other, group = await pair(lab, c)
        await approve(lab, c, max_gap_ns=str(6 * HOUR))
        path, params = routes(lab, other, group)[route]
        r = await c.get(path, params={**params, "layer": "corrected@v1"})
        assert r.status_code == 422, r.text
        [detail] = r.json()["detail"]
        assert detail["field"] == "layer" and "versions count per series" in detail["message"]
        for name in ("raw", "corrected@latest"):
            assert (await c.get(path, params={**params, "layer": name})).status_code == 200, name
