#!/usr/bin/env python3
"""Load well-known public time series into a Tabayyun install, or write them as CSV files.

The series come from their publishers at run time (spec 019): the Numenta Anomaly Benchmark,
the ETT transformer data, Open Power System Data, the UCI household power data and the Jena
weather station. Each download is checked against a pinned SHA-256, converted to Tabayyun's
`ts,value` CSV, and then either written to a folder or uploaded as a run:

    python3 deploy/examples/public.py --list
    python3 deploy/examples/public.py --out ./examples
    TABAYYUN_SESSION='<cookie value>' python3 deploy/examples/public.py \\
        --url https://tabayyun-stg.siralabs.org --only nab,ett

`--url` uploads each series, records its source and licence in the series metadata, runs the
ETT loads as a dataset (cross-series checks), and reports, for the NAB series, how many of
NAB's labelled anomaly windows the findings touch. Those labels mark operational events, not
data faults, so the count describes behaviour, not accuracy. The session works as in
`seed.py`. Standard library only.

Nothing from these sources is stored in the repository: ETT is CC BY-ND 4.0 and NAB needs its
notice. The converted files are for your own testing; do not redistribute the ETT extracts.
"""

# A command-line tool: it prints its progress and opens the fixed HTTPS URLs below.
# ruff: noqa: T201, S310

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sys
import tempfile
import zipfile
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib import error, request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed import Api, SeedError, findings_of_run, series_ids, wait_run  # noqa: E402

MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
# The api refuses larger uploads (services/runs.py, MAX_UPLOAD_BYTES).
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
CHUNK = 1 << 20

Row = tuple[str, str]
"""An ISO 8601 UTC timestamp and the value as text; an empty value is missing."""


@dataclass(frozen=True)
class Source:
    """One file at its publisher, pinned by checksum."""

    key: str
    url: str
    sha256: str
    title: str
    licence: str
    page: str


@dataclass(frozen=True)
class Options:
    """Choices that change what a converter keeps."""

    uci_year: int = 2007


Converter = Callable[[Path, Options], Iterator[Row]]


@dataclass(frozen=True)
class Series:
    """One series of the catalogue and how to read it from its source."""

    name: str
    family: str
    source: str
    convert: Converter
    unit: str | None = None
    physical_min: float | None = None
    physical_max: float | None = None
    notes: str = ""
    labels: str | None = None


# --- sources -----------------------------------------------------------------------------

NAB_RAW = "https://raw.githubusercontent.com/numenta/NAB/ea702d75cc2258d9d7dd35ca8e5e2539d71f3140"
ETT_RAW = "https://raw.githubusercontent.com/zhouhaoyi/ETDataset/1d16c8f4f943005d613b5bc962e9eeb06058cf07"
NAB_PAGE = "https://github.com/numenta/NAB"
NAB_LICENCE = "MIT, Copyright 2014-2024 Numenta Inc."


def _nab(key: str, path: str, sha256: str, title: str) -> Source:
    return Source(key, f"{NAB_RAW}/{path}", sha256, title, NAB_LICENCE, NAB_PAGE)


SOURCES: dict[str, Source] = {
    s.key: s
    for s in [
        _nab(
            "nab_machine_temperature.csv",
            "data/realKnownCause/machine_temperature_system_failure.csv",
            "92bf5b87fc7f9bba8ca0b7ec63ccaac8cb4a1371a258e8c29a10ae9c018d82a4",
            "NAB: temperature sensor on a large industrial machine, with a known breakdown",
        ),
        _nab(
            "nab_nyc_taxi.csv",
            "data/realKnownCause/nyc_taxi.csv",
            "d8fa6f7f0734bf5c8be12c52a94e20a82664c397d9dec4449156bd453d32856d",
            "NAB: New York City taxi passengers per 30 minutes",
        ),
        _nab(
            "nab_ambient_temperature.csv",
            "data/realKnownCause/ambient_temperature_system_failure.csv",
            "230b68ccca20f59d562afd5d24ad52939c9b784386bed0054018358bf9120581",
            "NAB: ambient temperature in an office, with a system failure",
        ),
        _nab(
            "nab_cpu_asg.csv",
            "data/realKnownCause/cpu_utilization_asg_misconfiguration.csv",
            "58ba65dc0737cfbac11b51514476d50c438d44011232144bb8d93f392df58f9f",
            "NAB: CPU utilisation of an AWS auto-scaling group, misconfigured",
        ),
        _nab(
            "nab_ec2_latency.csv",
            "data/realKnownCause/ec2_request_latency_system_failure.csv",
            "98378580aa80157e057c61d59d81daddccc6c65a2c0c800e3f01f603b8215c3f",
            "NAB: request latency of an AWS EC2 service, with a system failure",
        ),
        _nab(
            "nab_art_flatmiddle.csv",
            "data/artificialWithAnomaly/art_daily_flatmiddle.csv",
            "ea5df125947e9ee2b977ac3d341f3c2a029588862af97cae4a654fc56e838702",
            "NAB: artificial daily signal that goes flat in the middle",
        ),
        _nab(
            "nab_labels.json",
            "labels/combined_windows.json",
            "1e1fbc4601321aad8d0f8b3784c8134299379f68f6c1f7777565f8ffd57ab6b1",
            "NAB: labelled anomaly windows",
        ),
        Source(
            "ETTh1.csv",
            f"{ETT_RAW}/ETT-small/ETTh1.csv",
            "f18de3ad269cef59bb07b5438d79bb3042d3be49bdeecf01c1cd6d29695ee066",
            "ETT: hourly loads and oil temperature of electricity transformer 1, 2016-2018",
            "CC BY-ND 4.0 (Zhou et al., Informer, AAAI 2021)",
            "https://github.com/zhouhaoyi/ETDataset",
        ),
        Source(
            "opsd_60min.csv",
            "https://data.open-power-system-data.org/time_series/2020-10-06/time_series_60min_singleindex.csv",
            "6a7f2bc571314cbf9c321cc03437691cd4be95c3a6f075e60ff99e8035c704c8",
            "Open Power System Data: hourly load, wind and solar, release 2020-10-06",
            "CC BY 4.0, with attribution of the original sources (ENTSO-E Transparency, TSOs)",
            "https://doi.org/10.25832/time_series/2020-10-06",
        ),
        Source(
            "uci_household.zip",
            "https://archive.ics.uci.edu/static/public/235/individual+household+electric+power+consumption.zip",
            "9f84b46ade8a2d8e1286ec4b2b6c2987a45a755c59f263be3b3b3d10dfbda3ff",
            "UCI: individual household electric power consumption, 1-minute, 2006-2010",
            "CC BY 4.0 (Hebrail and Berard, UCI Machine Learning Repository)",
            "https://doi.org/10.24432/C58K54",
        ),
        Source(
            "jena_climate.zip",
            "https://storage.googleapis.com/tensorflow/tf-keras-datasets/jena_climate_2009_2016.csv.zip",
            "63d757501e92284a7de7cdbef0337f03b24e13ead7ac2b5b8c86a18d8e38ba5b",
            "Jena weather station, 10-minute, 2009-2016 (as packaged for Keras)",
            "attribution to the Max Planck Institute for Biogeochemistry, Jena",
            "https://www.bgc-jena.mpg.de/wetter/",
        ),
    ]
}


# --- converters --------------------------------------------------------------------------


def iso(t: datetime) -> str:
    """ISO 8601 UTC with second precision."""
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def number(text: str) -> str:
    """The value as written, or empty when it is missing or not a finite number."""
    text = text.strip()
    try:
        value = float(text)
    except ValueError:
        return ""
    return text if value == value and abs(value) != float("inf") else ""


def _naive(text: str, fmt: str) -> str:
    """A zone-less timestamp read as UTC."""
    return iso(datetime.strptime(text.strip(), fmt).replace(tzinfo=UTC))


def csv_column(ts_col: str, value_col: str, fmt: str) -> Converter:
    """One column of a plain CSV with naive timestamps in `fmt` (NAB, ETT)."""

    def convert(path: Path, _: Options) -> Iterator[Row]:
        with path.open(newline="", encoding="utf-8") as f:
            for rec in csv.DictReader(f):
                yield _naive(rec[ts_col], fmt), number(rec[value_col])

    return convert


def opsd_column(value_col: str) -> Converter:
    """One OPSD column, from its first to its last reported value (the file spans every
    country's coverage, so each column starts and ends with empty rows of its own)."""

    def convert(path: Path, _: Options) -> Iterator[Row]:
        pending: list[Row] = []
        started = False
        with path.open(newline="", encoding="utf-8") as f:
            for rec in csv.DictReader(f):
                row = (rec["utc_timestamp"], number(rec[value_col]))
                if not row[1]:
                    if started:
                        pending.append(row)
                    continue
                started = True
                yield from pending
                pending.clear()
                yield row

    return convert


def _zip_lines(path: Path, suffix: str) -> Iterator[str]:
    """The text lines of the one member of the zip file whose name ends with `suffix`."""
    with zipfile.ZipFile(path) as z:
        name = next((n for n in z.namelist() if n.endswith(suffix)), None)
        if name is None:
            raise SeedError(f"{path.name} has no member ending in {suffix}")
        with z.open(name) as raw:
            yield from io.TextIOWrapper(raw, encoding="utf-8", newline="")


def uci_power(path: Path, opts: Options) -> Iterator[Row]:
    """`Global_active_power` of the chosen year; `?` marks missing minutes."""
    year = f"/{opts.uci_year}"
    reader = csv.reader(_zip_lines(path, ".txt"), delimiter=";")
    header = next(reader)
    col = header.index("Global_active_power")
    for rec in reader:
        if len(rec) > col and rec[0].endswith(year):
            yield _naive(f"{rec[0]} {rec[1]}", "%d/%m/%Y %H:%M:%S"), number(rec[col])


def jena_column(value_col: str) -> Converter:
    """One column of the Jena weather file, whose dates read `dd.mm.YYYY HH:MM:SS`."""

    def convert(path: Path, _: Options) -> Iterator[Row]:
        for rec in csv.DictReader(_zip_lines(path, ".csv")):
            yield _naive(rec["Date Time"], "%d.%m.%Y %H:%M:%S"), number(rec[value_col])

    return convert


NAB_TS = "%Y-%m-%d %H:%M:%S"
NAB_NOTE = "NAB rows as published; timestamps without a zone, read as UTC."
LOCAL_NOTE = "Local time without a zone in the source, written as UTC."

CATALOGUE: list[Series] = [
    Series("nab-machine-temperature", "nab", "nab_machine_temperature.csv",
           csv_column("timestamp", "value", NAB_TS), notes=NAB_NOTE,
           labels="realKnownCause/machine_temperature_system_failure.csv"),
    Series("nab-nyc-taxi", "nab", "nab_nyc_taxi.csv", csv_column("timestamp", "value", NAB_TS),
           unit="passengers", physical_min=0, notes=NAB_NOTE, labels="realKnownCause/nyc_taxi.csv"),
    Series("nab-ambient-temperature", "nab", "nab_ambient_temperature.csv",
           csv_column("timestamp", "value", NAB_TS), notes=NAB_NOTE,
           labels="realKnownCause/ambient_temperature_system_failure.csv"),
    Series("nab-cpu-asg", "nab", "nab_cpu_asg.csv", csv_column("timestamp", "value", NAB_TS),
           unit="%", physical_min=0, physical_max=100, notes=NAB_NOTE,
           labels="realKnownCause/cpu_utilization_asg_misconfiguration.csv"),
    Series("nab-ec2-latency", "nab", "nab_ec2_latency.csv", csv_column("timestamp", "value", NAB_TS),
           physical_min=0, notes=NAB_NOTE, labels="realKnownCause/ec2_request_latency_system_failure.csv"),
    Series("nab-art-flatmiddle", "nab", "nab_art_flatmiddle.csv", csv_column("timestamp", "value", NAB_TS),
           notes=NAB_NOTE, labels="artificialWithAnomaly/art_daily_flatmiddle.csv"),
    Series("ett-h1-ot", "ett", "ETTh1.csv", csv_column("date", "OT", NAB_TS), unit="degC",
           notes="Oil temperature, the ETT target."),
    Series("ett-h1-hufl", "ett", "ETTh1.csv", csv_column("date", "HUFL", NAB_TS),
           notes="High useful load."),
    Series("ett-h1-mufl", "ett", "ETTh1.csv", csv_column("date", "MUFL", NAB_TS),
           notes="Middle useful load."),
    Series("ett-h1-lufl", "ett", "ETTh1.csv", csv_column("date", "LUFL", NAB_TS),
           notes="Low useful load."),
    Series("opsd-de-load", "opsd", "opsd_60min.csv", opsd_column("DE_load_actual_entsoe_transparency"),
           unit="MW", physical_min=0, notes="Germany, actual load (ENTSO-E Transparency)."),
    Series("opsd-de-solar", "opsd", "opsd_60min.csv", opsd_column("DE_solar_generation_actual"),
           unit="MW", physical_min=0, notes="Germany, actual solar generation; zero every night."),
    Series("opsd-de-wind", "opsd", "opsd_60min.csv", opsd_column("DE_wind_generation_actual"),
           unit="MW", physical_min=0, notes="Germany, actual wind generation."),
    Series("uci-household-power", "uci", "uci_household.zip", uci_power, unit="kW", physical_min=0,
           notes="Global active power of one household near Paris, one calendar year. " + LOCAL_NOTE),
    Series("jena-temperature", "jena", "jena_climate.zip", jena_column("T (degC)"), unit="degC",
           physical_min=-60, physical_max=60, notes=LOCAL_NOTE),
    Series("jena-pressure", "jena", "jena_climate.zip", jena_column("p (mbar)"), unit="mbar",
           physical_min=850, physical_max=1100, notes=LOCAL_NOTE),
    Series("jena-wind-speed", "jena", "jena_climate.zip", jena_column("wv (m/s)"), unit="m/s",
           physical_min=0, notes="The source marks failed readings as -9999. " + LOCAL_NOTE),
]  # fmt: skip

ETT_GROUP = ("ETT transformer 1 loads", ["ett-h1-hufl", "ett-h1-mufl", "ett-h1-lufl"])
FAMILIES = sorted({s.family for s in CATALOGUE})


def select(only: str | None) -> list[Series]:
    """The catalogue, or the named series and families in catalogue order."""
    if not only:
        return list(CATALOGUE)
    wanted = {w.strip() for w in only.split(",") if w.strip()}
    known = set(FAMILIES) | {s.name for s in CATALOGUE}
    unknown = sorted(wanted - known)
    if unknown:
        raise SeedError(f"unknown --only {', '.join(unknown)}; valid: {', '.join(sorted(known))}")
    return [s for s in CATALOGUE if s.name in wanted or s.family in wanted]


# --- downloads ---------------------------------------------------------------------------

Fetcher = Callable[[str, Path], None]
"""Writes the body at a URL to a path; the default streams over HTTPS."""


def http_fetch(url: str, dest: Path) -> None:
    """Stream `url` to `dest`, refusing anything that is not HTTPS or exceeds the cap."""
    if not url.startswith("https://"):
        raise SeedError(f"refusing a non-HTTPS source: {url}")
    size = 0
    try:
        with request.urlopen(url, timeout=120) as resp, dest.open("wb") as out:
            while chunk := resp.read(CHUNK):
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise SeedError(f"{url} is larger than {MAX_DOWNLOAD_BYTES // 2**20} MiB")
                out.write(chunk)
    except (error.URLError, TimeoutError) as exc:
        raise SeedError(f"download of {url} failed: {exc}") from exc


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def ensure(source: Source, cache: Path, fetch: Fetcher) -> Path:
    """The source in the cache, downloaded if absent or changed; a checksum mismatch is fatal."""
    dest = cache / source.key
    if dest.is_file() and sha256_of(dest) == source.sha256:
        return dest
    cache.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=cache, prefix=f".{source.key}.")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        print(f"  downloading {source.key} from {source.url}")
        fetch(source.url, tmp)
        got = sha256_of(tmp)
        if got != source.sha256:
            raise SeedError(f"{source.key}: checksum {got} is not the pinned {source.sha256}")
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest


def default_cache() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "tabayyun" / "examples"


# --- CSV, labels, report -----------------------------------------------------------------


@dataclass
class Built:
    """A converted series: its CSV and the span of its timestamps."""

    series: Series
    data: bytes
    rows: int
    first: str
    last: str


def build(series: Series, path: Path, opts: Options) -> Built:
    """The series as `ts,value` CSV; refuses one the api would reject for its size."""
    out = io.StringIO()
    out.write("ts,value\n")
    rows, first, last = 0, "", ""
    for ts, value in series.convert(path, opts):
        out.write(f"{ts},{value}\n")
        rows += 1
        first = first or ts
        last = ts
    data = out.getvalue().encode()
    if len(data) > MAX_UPLOAD_BYTES:
        raise SeedError(f"{series.name} is {len(data) / 2**20:.0f} MiB, over the 50 MiB upload limit")
    if not rows:
        raise SeedError(f"{series.name} has no rows (check --uci-year)")
    return Built(series, data, rows, first, last)


def ns(text: str) -> int:
    """ns since the epoch of a UTC timestamp (ISO 8601 or NAB's label format)."""
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return int(datetime.strptime(text, fmt).replace(tzinfo=UTC).timestamp()) * 10**9
        except ValueError:
            continue
    raise SeedError(f"unreadable timestamp {text!r}")


def label_windows(labels: dict[str, list[list[str]]], key: str) -> list[tuple[int, int]]:
    return [(ns(a), ns(b)) for a, b in labels.get(key, [])]


def label_hits(windows: list[tuple[int, int]], findings: list[dict]) -> tuple[int, int]:
    """(windows some finding overlaps, findings that overlap no window)."""

    def overlaps(f: dict, w: tuple[int, int]) -> bool:
        return bool(f["window"]["start"] <= w[1] and f["window"]["end"] >= w[0])

    hit = sum(1 for w in windows if any(overlaps(f, w) for f in findings))
    outside = sum(1 for f in findings if not any(overlaps(f, w) for w in windows))
    return hit, outside


def sources_md(built: Iterable[Built]) -> str:
    """Attribution for the written files."""
    lines = [
        "# Sources of these example series",
        "",
        "Written by `deploy/examples/public.py` (spec 019) for your own testing. Each file is",
        "`ts,value` (ISO 8601 UTC); upload it with the defaults of the upload form. Do not",
        "redistribute the ETT extracts (CC BY-ND 4.0).",
        "",
        "| File | Rows | From | To | Source | Licence | Notes |",
        "|---|---|---|---|---|---|---|",
    ]
    for b in built:
        src = SOURCES[b.series.source]
        lines.append(
            f"| `{b.series.name}.csv` | {b.rows} | {b.first} | {b.last} | [{src.title}]({src.page}) "
            f"| {src.licence} | {b.series.notes} |"
        )
    return "\n".join(lines) + "\n"


# --- modes -------------------------------------------------------------------------------


def list_catalogue() -> None:
    for s in CATALOGUE:
        src = SOURCES[s.source]
        print(f"{s.family:5} {s.name:25} {s.unit or '-':10} {src.title}")


def write_out(built: list[Built], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for b in built:
        (out / f"{b.series.name}.csv").write_bytes(b.data)
        print(f"  wrote {b.series.name}.csv ({b.rows} rows, {b.first} to {b.last})")
    (out / "SOURCES.md").write_text(sources_md(built), encoding="utf-8")
    print(f"  wrote SOURCES.md; upload the files from {out} with the form's defaults")


def upload_all(api: Api, built: list[Built], labels: dict[str, list[list[str]]]) -> bool:
    """Upload, describe, group and report; True when every run succeeded."""
    ok = True
    by_series: dict[str, list[dict]] = {}
    for b in built:
        s = b.series
        fields = {"series_id": s.name}
        if s.unit:
            fields["unit"] = s.unit
        if s.physical_min is not None:
            fields["physical_min"] = str(s.physical_min)
        if s.physical_max is not None:
            fields["physical_max"] = str(s.physical_max)
        created = api.post_form("/api/runs", fields, f"{s.name}.csv", b.data)
        run = wait_run(api, created["id"])
        print(f"  upload {s.name:25} {b.rows:>7} rows  run {run['id']} {run['status']}")
        ok &= run["status"] == "succeeded"
        by_series[s.name] = findings_of_run(api, run["id"])

    ids = series_ids(api)
    for b in built:
        if b.series.name not in ids:  # its run failed before the series was stored
            continue
        src = SOURCES[b.series.source]
        meta = {"source": src.title, "url": src.url, "licence": src.licence, "notes": b.series.notes}
        api.patch_json(f"/api/series/{ids[b.series.name]}", {"metadata": {"example": meta}})

    names = {b.series.name: b for b in built}
    group_name, members = ETT_GROUP
    if all(m in names and m in ids for m in members):
        group = api.post_json(
            "/api/series-groups",
            {"name": group_name, "kind": "related",
             "members": [{"series_id": ids[m], "role": "member"} for m in members]},
        )  # fmt: skip
        first = min(names[m].first for m in members)
        last = max(names[m].last for m in members)
        end = iso(datetime.fromtimestamp(ns(last) / 1e9, tz=UTC) + timedelta(hours=1))
        dataset = api.post_json(
            "/api/datasets",
            {"name": "Public examples: ETT", "series_ids": [ids[m] for m in members],
             "window": {"start": first, "end": end}},
        )  # fmt: skip
        created = api.post_json("/api/runs", {"dataset_id": dataset["id"]})
        run = wait_run(api, created["id"])
        print(f"  group {group['name']}: dataset run {run['id']} {run['status']}")
        ok &= run["status"] == "succeeded"
        uuid_to_name = {v: k for k, v in ids.items()}
        for f in findings_of_run(api, run["id"]):
            by_series.setdefault(uuid_to_name.get(f["series_id"], "?"), []).append(f)

    print("findings by series")
    for name, found in by_series.items():
        counts = Counter(f["check_id"] for f in found)
        summary = ", ".join(f"{c} {k}" for k, c in counts.most_common()) or "none"
        print(f"  {name:25} {summary}")
        key = names[name].series.labels if name in names else None
        if key:
            windows = label_windows(labels, key)
            hit, outside = label_hits(windows, found)
            print(f"  {'':25} NAB labels touched: {hit} of {len(windows)}; findings outside them: {outside}")
    return ok


def main(argv: list[str] | None = None, fetch: Fetcher = http_fetch) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true", help="print the catalogue")
    mode.add_argument("--out", type=Path, help="write ts,value CSVs and SOURCES.md to this folder")
    mode.add_argument("--url", help="upload to this install, e.g. https://tabayyun-stg.siralabs.org")
    parser.add_argument("--only", help=f"comma-separated series or families ({', '.join(FAMILIES)})")
    parser.add_argument("--uci-year", type=int, default=2007, help="year of the UCI series (2007-2010)")
    parser.add_argument("--cache", type=Path, default=default_cache(), help="download folder")
    args = parser.parse_args(argv)

    if args.list:
        list_catalogue()
        return 0
    chosen = select(args.only)
    opts = Options(uci_year=args.uci_year)
    api = Api(args.url, os.environ.get("TABAYYUN_SESSION") or None) if args.url else None
    if api is not None:
        # Before the downloads, so a wrong URL or session fails fast.
        version = api.get("/api/version")
        print(f"install {args.url}: commit {(version.get('commit') or '?')[:7]}")

    print(f"sources (cache {args.cache})")
    paths = {key: ensure(SOURCES[key], args.cache, fetch) for key in dict.fromkeys(s.source for s in chosen)}
    labels: dict[str, list[list[str]]] = {}
    if any(s.labels for s in chosen):
        labels = json.loads(ensure(SOURCES["nab_labels.json"], args.cache, fetch).read_text("utf-8"))
    built = [build(s, paths[s.source], opts) for s in chosen]

    if args.out:
        write_out(built, args.out)
        return 0
    if api is None:  # argparse requires one mode; unreachable
        raise SeedError("one of --list, --out and --url is required")
    return 0 if upload_all(api, built, labels) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SeedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
