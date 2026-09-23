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

- `--ts-unit auto|s|ms|us|ns` (default `auto`) on `check`, `profile` and `check-multi`.
- `tabayyun_core::time::{infer_epoch_unit(median_abs: i64) -> TsUnit, epoch_to_ns(v, unit)
  -> Option<i64>, PLAUSIBLE_RANGE_NS}`, used by the CLI; the Python API keeps its own copy
  and a shared test table asserts both agree.
- JSON output gains `ts_unit` (`s`, `ms`, `us`, `ns` or `text`).

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

- [ ] Under `auto`, an integer column with median \|value\| below 1e11 is read as seconds in
      every row: a stray cell that per-cell inference would have read as milliseconds
      (e.g. 1 700 000 000 000) is read as seconds too, falls outside the plausible range and
      exits 2 naming its row.
- [ ] A declared wrong unit that lands outside the range exits 2 with the message.
- [ ] The shared table of (value, unit) → ns cases passes in Rust and in `api/tests`.
- [ ] `ts_unit` appears in the CLI JSON output.

## Test cases

Unit (`time::tests`): `infer_thresholds`, `range_check`. CLI (`tabayyun-cli` tests):
`column_unit_is_uniform`, `wrong_unit_exits_2`. API: `tests/test_ts_unit.py` reads the shared
table from `core/tabayyun-core/tests/data/epoch_cases.json`.

## Out of scope

- Per-row units or mixed columns (rejected, not guessed).
