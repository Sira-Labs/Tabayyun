"""`deploy/examples/public.py` (spec 019): catalogue, converters, label scoring and the
download and size guards, without network access."""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "examples" / "public.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("public_examples", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


pub = _load()
OPTS = pub.Options()


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _zip(path: Path, member: str, text: str) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(member, text)
    return path


def test_catalogue_is_consistent() -> None:
    names = [s.name for s in pub.CATALOGUE]
    assert len(names) == len(set(names))
    for s in pub.CATALOGUE:
        assert s.source in pub.SOURCES, s.name
    for src in pub.SOURCES.values():
        assert src.url.startswith("https://"), src.key
        assert len(src.sha256) == 64 and int(src.sha256, 16) >= 0, src.key
        assert src.licence and src.page.startswith("https://"), src.key
    assert all(m in names for m in pub.ETT_GROUP[1])


def test_nab_rows(tmp_path: Path) -> None:
    path = _write(tmp_path / "n.csv", "timestamp,value\n2014-07-01 00:00:00,10844\n2014-07-01 00:30:00,\n")
    rows = list(pub.csv_column("timestamp", "value", pub.NAB_TS)(path, OPTS))
    assert rows == [("2014-07-01T00:00:00Z", "10844"), ("2014-07-01T00:30:00Z", "")]


def test_ett_column(tmp_path: Path) -> None:
    path = _write(tmp_path / "e.csv", "date,HUFL,OT\n2016-07-01 00:00:00,5.827,30.531\n")
    assert list(pub.csv_column("date", "OT", pub.NAB_TS)(path, OPTS)) == [("2016-07-01T00:00:00Z", "30.531")]


def test_opsd_trims_empty_ends(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "o.csv",
        "utc_timestamp,X\n2015-01-01T00:00:00Z,\n2015-01-01T01:00:00Z,5\n"
        "2015-01-01T02:00:00Z,\n2015-01-01T03:00:00Z,7\n2015-01-01T04:00:00Z,\n",
    )
    rows = list(pub.opsd_column("X")(path, OPTS))
    # Leading and trailing empties go; the gap inside stays as a missing value.
    assert rows == [
        ("2015-01-01T01:00:00Z", "5"),
        ("2015-01-01T02:00:00Z", ""),
        ("2015-01-01T03:00:00Z", "7"),
    ]


def test_uci_joins_date_and_time_and_year(tmp_path: Path) -> None:
    text = (
        "Date;Time;Global_active_power;Voltage\n"
        "31/12/2006;23:59:00;1.5;240\n"
        "1/1/2007;00:00:00;2.580;241\n"
        "1/1/2007;00:01:00;?;?\n"
    )
    path = _zip(tmp_path / "u.zip", "household_power_consumption.txt", text)
    rows = list(pub.uci_power(path, pub.Options(uci_year=2007)))
    assert rows == [("2007-01-01T00:00:00Z", "2.580"), ("2007-01-01T00:01:00Z", "")]


def test_jena_dates(tmp_path: Path) -> None:
    text = '"Date Time","p (mbar)","wv (m/s)"\n01.01.2009 00:10:00,996.52,-9999.00\n'
    path = _zip(tmp_path / "j.zip", "jena_climate_2009_2016.csv", text)
    assert list(pub.jena_column("wv (m/s)")(path, OPTS)) == [("2009-01-01T00:10:00Z", "-9999.00")]


def test_number_drops_non_finite_and_text() -> None:
    assert [pub.number(t) for t in ("1.5", " 2 ", "?", "", "nan", "inf")] == ["1.5", "2", "", "", "", ""]


def test_label_windows_hit() -> None:
    labels = {"k.csv": [["2014-01-01 00:00:00.000000", "2014-01-02 00:00:00.000000"],
                        ["2014-02-01 00:00:00.000000", "2014-02-02 00:00:00.000000"]]}  # fmt: skip
    windows = pub.label_windows(labels, "k.csv")
    inside = {"window": {"start": pub.ns("2014-01-01T12:00:00Z"), "end": pub.ns("2014-01-01T13:00:00Z")}}
    elsewhere = {"window": {"start": pub.ns("2014-03-01T00:00:00Z"), "end": pub.ns("2014-03-02T00:00:00Z")}}
    assert pub.label_hits(windows, [inside, elsewhere]) == (1, 1)
    assert pub.label_hits(windows, []) == (0, 0)


def _source(body: bytes) -> object:
    return pub.Source("s.csv", "https://example.test/s.csv", hashlib.sha256(body).hexdigest(), "t", "l", "p")


def test_checksum_mismatch_is_refused(tmp_path: Path) -> None:
    src = _source(b"expected")

    def fetch(_url: str, dest: Path) -> None:
        dest.write_bytes(b"tampered")

    with pytest.raises(pub.SeedError, match="checksum"):
        pub.ensure(src, tmp_path, fetch)
    assert list(tmp_path.iterdir()) == []  # no partial file left behind


def test_cached_source_is_not_fetched_again(tmp_path: Path) -> None:
    src = _source(b"body")
    calls: list[str] = []

    def fetch(url: str, dest: Path) -> None:
        calls.append(url)
        dest.write_bytes(b"body")

    assert pub.ensure(src, tmp_path, fetch).read_bytes() == b"body"
    pub.ensure(src, tmp_path, fetch)
    assert len(calls) == 1


def test_oversized_csv_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pub, "MAX_UPLOAD_BYTES", 40)
    path = _write(tmp_path / "n.csv", "timestamp,value\n" + "2014-07-01 00:00:00,1\n" * 5)
    series = pub.Series("big", "nab", "n.csv", pub.csv_column("timestamp", "value", pub.NAB_TS))
    with pytest.raises(pub.SeedError, match="upload limit"):
        pub.build(series, path, OPTS)


def test_http_fetch_refuses_plain_http(tmp_path: Path) -> None:
    with pytest.raises(pub.SeedError, match="non-HTTPS"):
        pub.http_fetch("http://example.test/x.csv", tmp_path / "x")


def test_only_selects_families() -> None:
    names = [s.name for s in pub.select("ett,nab-nyc-taxi")]
    assert names == ["nab-nyc-taxi", "ett-h1-ot", "ett-h1-hufl", "ett-h1-mufl", "ett-h1-lufl"]
    assert len(pub.select(None)) == len(pub.CATALOGUE)


def test_unknown_only_name_exits_2() -> None:
    # The script maps SeedError to exit status 2.
    with pytest.raises(pub.SeedError, match="unknown --only nope"):
        pub.select("nab,nope")


def test_out_writes_csvs_and_sources(tmp_path: Path) -> None:
    nab = "timestamp,value\n2014-07-01 00:00:00,10844\n2014-07-01 00:30:00,8127\n"
    labels = json.dumps({"realKnownCause/nyc_taxi.csv": []})
    bodies = {"nab_nyc_taxi.csv": nab.encode(), "nab_labels.json": labels.encode()}
    sources = dict(pub.SOURCES)
    for key, body in bodies.items():
        sources[key] = dataclasses.replace(sources[key], sha256=hashlib.sha256(body).hexdigest())
    by_url = {sources[k].url: v for k, v in bodies.items()}

    def fetch(url: str, dest: Path) -> None:
        dest.write_bytes(by_url[url])

    pub.SOURCES, saved = sources, pub.SOURCES
    try:
        out = tmp_path / "out"
        code = pub.main(["--out", str(out), "--only", "nab-nyc-taxi", "--cache", str(tmp_path / "c")], fetch)
    finally:
        pub.SOURCES = saved
    assert code == 0
    csv_text = (out / "nab-nyc-taxi.csv").read_text()
    assert csv_text == "ts,value\n2014-07-01T00:00:00Z,10844\n2014-07-01T00:30:00Z,8127\n"
    sources_md = (out / "SOURCES.md").read_text()
    assert "nab-nyc-taxi.csv" in sources_md and "MIT" in sources_md and "CC BY-ND" in sources_md


def test_list_prints_every_series(capsys: pytest.CaptureFixture[str]) -> None:
    assert pub.main(["--list"]) == 0
    printed = capsys.readouterr().out
    assert all(s.name in printed for s in pub.CATALOGUE)


def test_zip_without_member_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "z.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("other.bin", io.BytesIO(b"x").getvalue())
    with pytest.raises(pub.SeedError, match="no member"):
        list(pub.uci_power(path, OPTS))
