# Spec 017 — CLI epoch timestamp units match the API (ADR-0014)

Sprint 7, story S7-10 (C). Depends on: ADR-0014. Packages: `core/tabayyun-cli/src/main.rs`,
`core/tabayyun-core/src/time.rs`.

## Goal

The CLI and the API read the same epoch-integer file to the same instants. Today the CLI
infers the unit per cell from its magnitude, so a column can mix units near a threshold, and
it accepts instants outside the plausible range; the API infers once per column from the
median magnitude and rejects instants outside 1971-01-01 to 2200-01-01.

## User story

As a data engineer, I get the same findings for a file whether I check it with the CLI in
CI or upload it in the browser.

## Interface

- `--ts-unit auto|s|ms|us|ns` (default `auto`) on `run`, `check-multi` and `cache write` (the
  commands that read a file; see the edits).
- `tabayyun_core::time::{infer_epoch_unit(median_abs: i64) -> TsUnit, epoch_to_ns(v, unit)
  -> Option<i64>, PLAUSIBLE_RANGE_NS}`, used by the CLI; the Python API keeps its own copy
  and a shared test table asserts both agree.
- JSON output of `run` and `check-multi` gains `ts_unit` (`s`, `ms`, `us`, `ns` or `text`).

## Behaviour

1. A timestamp column whose cells are all integers is epoch; `auto` takes the unit from the
   median absolute value m with the ADR-0014 thresholds as half-open ranges, exactly as the
   API's `infer_epoch_unit`: m < 1e11 → s; 1e11 ≤ m < 1e14 → ms; 1e14 ≤ m < 1e17 → us;
   m ≥ 1e17 → ns. The shared test table includes each boundary value. Mixed text and
   integers is an error naming the first offending row.
2. Every converted instant must fall in `[1971-01-01, 2200-01-01)`; otherwise exit code 2 with
   the row, the value and the unit read, and a hint to pass `--ts-unit`.
3. Text timestamps parse as today; `ts_unit` is `text`.

## Acceptance criteria

- [x] Under `auto`, an integer column with median \|value\| below 1e11 is read as seconds in
      every row: a stray cell that per-cell inference would have read as milliseconds
      (e.g. 1 700 000 000 000) is read as seconds too, falls outside the plausible range and
      exits 2 naming its row.
- [x] A declared wrong unit that lands outside the range exits 2 with the message.
- [x] The shared table of (value, unit) → ns cases passes in Rust and in `api/tests`.
- [x] `ts_unit` appears in the CLI JSON output.

## Test cases

Unit (`time::tests`): `infer_thresholds`, `range_check`, `column_takes_one_unit_from_its_median`.
CLI (`tabayyun-cli/tests/ts_unit.rs`): `column_unit_is_uniform`, `wrong_unit_exits_2`,
`ts_unit_in_output`, `mixed_text_and_integers_exit_2`, `parquet_integer_seconds`. API:
`tests/test_ts_unit.py` reads the shared table from
`core/tabayyun-core/tests/data/epoch_cases.json` (`test_thresholds_match_the_core`,
`test_conversion_matches_the_core`).

## Implementation edits

Recorded on 2026-09-24; approved with the plan.

- The spec named CLI commands `check` and `profile`; the CLI has `run` (with `--profile`),
  `check-multi` and `cache write`, which all read files, so `--ts-unit` is on those three.
- Parquet: an `Int64` timestamp column was read as raw nanoseconds, the same 1970 trap as CSV
  integers before ADR-0014; it now follows the same rule. Typed timestamp columns keep their
  own unit, reported as `ts_unit`.
- Exit code 2 is shared with clap's usage errors: both mean the input is wrong, and the
  message says which. Other errors still exit 1.
- The ingest-time column follows the same rule with its own inference, as in the API. An empty
  integer column reads as nanoseconds, as the API's does.
- The median is exact in Rust (lower middle for an even count) and approximate in the API
  (`pc.approximate_median`); they can only differ for a column whose median sits on a
  threshold, which the range check then catches.
- The API test that scraped the thresholds from the CLI's `parse_ts` source is replaced by
  the shared table; `parse_ts` is now `parse_text_ts` and no longer reads integers.

## Out of scope

- Per-row units or mixed columns (rejected, not guessed).
