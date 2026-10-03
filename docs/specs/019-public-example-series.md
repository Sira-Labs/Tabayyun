# Spec 019 — Public example series

Sprint 8 side story (owner request, 3 Oct 2026). Depends on: 002 (runs), 004 (series metadata),
008 (series groups, datasets). Packages: `deploy/examples/`, `api/tests/`, `deploy/README.md`.

## Goal

`deploy/examples/public.py` downloads well-known public time series from their original
publishers, checks their checksums, converts them to Tabayyun's `ts,value` CSV, and either
writes the files to a folder or uploads them to an install as runs. The series metadata records
the source, licence and link. One dataset run covers the cross-series checks. For the Numenta
Anomaly Benchmark (NAB) series, the script reports how many labelled anomaly windows the
findings touch. Nothing from these sources is committed to the repository, because two of their
licences forbid that: ETT is CC BY-ND, and NAB's data needs its notice.

## User story

As the owner testing Tabayyun on staging, I load real, publicly known series in one command, so
that I can see how the checks behave on data other people have studied, and not only on our
synthetic faults.

## Interface

```
python3 deploy/examples/public.py --list
python3 deploy/examples/public.py --out DIR [--only NAMES] [--uci-year YEAR]
TABAYYUN_SESSION=... python3 deploy/examples/public.py --url URL [--only NAMES] [--uci-year YEAR]
```

- `--only`: comma-separated series names or families (`nab`, `ett`, `opsd`, `uci`, `jena`);
  all by default.
- `--uci-year`: the calendar year of the UCI household series, default 2007. The full file is
  over the 50 MiB upload limit.
- `--cache`: the download folder. Defaults to `$XDG_CACHE_HOME/tabayyun/examples`, otherwise
  `~/.cache/tabayyun/examples`. A file already there with the right checksum is not downloaded
  again.
- Standard library only, like `seed.py`, whose API client it reuses.

Catalogue (series name → source column, unit, limits):

| Family | Series | Source | Licence |
|---|---|---|---|
| nab | `nab-machine-temperature`, `nab-nyc-taxi`, `nab-ambient-temperature`, `nab-cpu-asg`, `nab-ec2-latency`, `nab-art-flatmiddle` | numenta/NAB at commit `ea702d7`, `value` | MIT (Numenta) |
| ett | `ett-h1-ot`, `ett-h1-hufl`, `ett-h1-mufl`, `ett-h1-lufl` | zhouhaoyi/ETDataset at commit `1d16c8f`, `ETTh1.csv` | CC BY-ND 4.0 |
| opsd | `opsd-de-load`, `opsd-de-solar`, `opsd-de-wind` | Open Power System Data time series 2020-10-06, 60 min | CC BY 4.0 (with source attribution) |
| uci | `uci-household-power` | UCI dataset 235, `Global_active_power`, one year | CC BY 4.0 |
| jena | `jena-temperature`, `jena-pressure`, `jena-wind-speed` | Max Planck Institute for Biogeochemistry, Jena weather station 2009–2016 (as packaged by Keras) | attribution, see source |

Series metadata set after upload (`PATCH /api/series/{id}`):
`{"example": {"source", "url", "licence", "notes"}}`.

Group and dataset in `--url` mode: the series group "ETT transformer 1 loads" (kind `related`,
members HUFL, MUFL and LUFL) and the dataset "Public examples: ETT" over the ETT window,
followed by one dataset run.

## Behaviour

1. `--list` prints the catalogue and exits 0. Exactly one of `--list`, `--out` and `--url` is
   required, otherwise exit 2 (argparse).
2. An unknown `--only` name exits 2 and lists the valid names.
3. Each needed source is downloaded over HTTPS once, into a temporary file in the cache, with
   at most 200 MB per file. A redirect to a URL that is not HTTPS is refused. Its SHA-256 is compared with the pinned value, and the file is
   moved into place only if it matches. A mismatch, an HTTP error or an oversized file exits 2
   and names the source.
4. Converters stream the source and yield `(timestamp, value)` rows: ISO 8601 UTC timestamps,
   with an empty value for missing data.
   - NAB keeps its rows as they are.
   - ETT takes one column.
   - OPSD takes one column. The script keeps the rows from the first non-empty value of each
     column to the last non-empty one.
   - UCI joins `Date` (d/m/Y) and `Time`, reads `?` as missing, and keeps the chosen year.
   - Jena converts `dd.mm.YYYY HH:MM:SS`.
   - UCI and Jena record local time without a zone. The script writes it as UTC and says so in
     the notes.
5. A CSV over 50 MiB exits 2 before any upload and names the series.
6. `--out DIR` writes one `<series>.csv` per series and a `SOURCES.md` with each source,
   licence, link and conversion note, then exits 0.
7. `--url` does the following:
   - Uploads each series through `POST /api/runs`, with its unit and physical limits, and waits
     for the run.
   - Sets the metadata on each series, found by the series id its run returns, never by name.
   - On a second run, reuses the ETT group and dataset of the same name.
   - Creates the ETT group and dataset and runs the dataset when every ETT series is selected.
   - Prints the findings per series by check.
   - Prints, for each NAB series, the number of labelled windows that some finding overlaps,
     and the number of findings outside every window.

   It exits 1 if a run failed and 0 otherwise. Label recall is information, not a pass mark.
8. As in `seed.py`, the session cookie is only sent to an `https://` URL, and HTTP errors exit
   2 with the status and the API's detail.

## Acceptance criteria

- [x] `--list`, `--out` and `--url` behave as above; the CSVs upload without change through
      the web form (`ts` / `value`).
- [x] Checksums are pinned for every source, and a mismatch or oversized download is refused.
- [x] Unit tests cover every converter on sample rows, the NAB label scoring, the size guard,
      the checksum check, the `--only` selection and `--out` with an injected fetcher.
- [x] Run against a local stack: every run succeeds; results recorded below.
- [x] `deploy/README.md` and the seed docstring point to the script; `TASKS.md` updated.

## Test cases

Unit (`api/tests/test_public_examples.py`, loading `deploy/examples/public.py`):
`test_catalogue_is_consistent`, `test_nab_rows`, `test_ett_column`, `test_opsd_trims_empty_ends`,
`test_uci_joins_date_and_time_and_year`, `test_jena_dates`, `test_label_windows_hit`,
`test_checksum_mismatch_is_refused`, `test_oversized_csv_is_refused`, `test_only_selects_families`,
`test_unknown_only_name_exits_2`, `test_out_writes_csvs_and_sources`.

## Out of scope

- An in-app "example data" picker, which would need a server-side fetch with an allow-list.
  This waits for the owner's call after this spec.
- Petrobras 3W, whose Parquet files would need pyarrow and so break the standard-library-only
  rule. Spec 009 measured it with the CLI.
- Scoring Tabayyun as an anomaly detector. NAB labels mark operational events, not data-quality
  faults, so recall here describes behaviour, not accuracy. A benchmark corpus is planned for
  Sprint 18.

## Measurement (local stack, 3 Oct 2026)

Ran `public.py --url http://localhost:8077` against a fresh database at `main` with this
branch (dev auth, inline jobs). The full catalogue took 1 min 47 s, of which about 34 s was
conversion.

**Bug found and fixed:** the three Jena runs failed at first with "number of parameters must
be between 0 and 65535". `persist_report` wrote every metric point in a single `INSERT`, and
eight years of daily points over several checks exceed PostgreSQL's per-statement limit. The
insert now goes in batches of 5,000 rows, with the test
`test_more_metric_points_than_one_statement_binds`. After the fix every run succeeded,
including the ETT dataset run.

| Series | Findings by check | Reading |
|---|---|---|
| `nab-machine-temperature` | 4 drift, 2 timestamp, 1 changepoint, 1 scale shift, 1 rate of change | 4 of 4 NAB windows touched, 2 findings outside |
| `nab-nyc-taxi` | 7 spikes, 1 changepoint, 1 rate of change | 3 of 5 windows (the holiday dips); 6 spikes outside |
| `nab-ambient-temperature` | 9 completeness, 2 spikes, 1 changepoint | 2 of 2 windows; the completeness gaps are real holes in the file |
| `nab-cpu-asg` | 4 drift, 3 changepoint, 1 noise, 1 spikes | 1 of 1 window |
| `nab-ec2-latency` | 3 spikes, 1 completeness, 1 timestamp, 1 drift | 3 of 3 windows |
| `nab-art-flatmiddle` | 2 flatline, 1 drift, 1 noise, 1 resolution loss | 1 of 1 window: the planted flat stretch |
| `ett-h1-ot` | 19 seasonality break, 14 flatline, 14 changepoint, 1 spikes | flatlines are whole days of identical values |
| `ett-h1-hufl/-mufl/-lufl` | 30/30/32 flatline, 24/20/22 changepoint; 157 correlation break (HUFL, dataset run) | the same 24-sample repeated days in every column, which looks like filled data in the source; the pair check is very chatty on these loads |
| `opsd-de-load/-solar/-wind` | 13/1/1 changepoint, 2/1/1 spikes, 0/4/3 completeness | quiet, as in spec 012; solar's nightly zeros are not flagged |
| `uci-household-power` (2007) | 18 drift, 5 completeness, 3 changepoint, 2 noise, 1 spikes | the completeness gaps match the `?` stretches |
| `jena-temperature/-pressure` | 17/14 noise, 16/1 changepoint, 8/3 flatline, 1 timestamp each | the timestamp finding is the source's duplicate rows |
| `jena-wind-speed` | 103 drift, 10 noise, 6 flatline, 1 physical range (18 values at -9999) | the -9999 sentinels are found, but they also drive the drift findings |

Follow-ups, not fixed here: `tby.distribution_drift` and `tby.noise_level` should ignore values
outside the physical limits, which the Jena -9999 values show. `tby.correlation_break` is too
chatty on ETT's daily-segment loads. Both are in `TASKS.md`.
