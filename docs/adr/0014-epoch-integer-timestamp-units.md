# ADR-0014: Epoch integer timestamps are read in a declared or inferred unit, then checked

- **Status:** Accepted
- **Date:** 2026-09-22
- **Deciders:** core maintainers

## Context
Uploaded CSV files carry timestamps either as text (RFC 3339 or naive `YYYY-MM-DD HH:MM:SS`)
or as epoch integers. Text is unambiguous; an integer is not: `1700000000` is a date in 2023
in seconds and a moment in January 1970 in nanoseconds. The API read every integer as
nanoseconds, while the `tabayyun` CLI inferred the unit from the magnitude. A production check
on 2026-09-22 uploaded hourly epoch seconds and got a 19,705-day gap and 0 % completeness:
wrong by a factor of 10⁹, yet parsed without complaint, which is the worst kind of error for a
data-quality tool.

Established ingestion tools make the unit explicit: InfluxDB's write API takes `precision`
(s, ms, us, ns), Telegraf's CSV parser `csv_timestamp_format = "unix" | "unix_ms" | "unix_us" |
"unix_ns"`, Elasticsearch date fields `epoch_second` / `epoch_millis`, pandas `to_datetime(unit=)`
and Kafka Connect's TimestampConverter `unix.precision`. None of them guesses silently.

## Decision
1. Upload endpoints (`POST /api/runs`, `POST /api/checks/run`) take `ts_unit` in
   `auto | s | ms | us | ns`, default `auto`. It applies to the timestamp column and the
   ingest-time column; text columns ignore it.
2. `auto` infers the unit from the median absolute value with the CLI's thresholds
   (< 10¹¹ seconds, < 10¹⁴ milliseconds, < 10¹⁷ microseconds, else nanoseconds), so the API
   and the CLI read the same file the same way. A test pins the thresholds to the CLI source.
3. After conversion, every epoch-integer instant must fall within 1971-01-01 to 2199-12-31.
   Anything else, or an overflow, is rejected (HTTP 422) with the dates it produced and a
   hint to set `ts_unit` or use RFC 3339 text. An instant in 1970 almost always means a unit
   error; process historians postdate 1971, and older data can be uploaded as text.
4. The unit that was read is recorded with the run (`stats.ts_unit`: `s`, `ms`, `us`, `ns`,
   or `text`), so a surprising inference is visible after the fact.

## Alternatives considered
| Option | Pros | Cons | Why not |
|---|---|---|---|
| Keep nanoseconds only | No change | Every epoch-seconds file is silently wrong | The bug this ADR fixes |
| Explicit unit, required for integers | No guessing at all | Every CLI-style file needs a form field; breaks the CLI's behaviour | `auto` plus a range check catches the mistakes guessing could make |
| Infer only, no parameter | Simple form | Data from 1970–1973 or in unusual units cannot be expressed | The parameter is the escape hatch |
| Per-value inference (CLI style) | Handles mixed files | A mixed-unit column is itself a quality problem, hidden by per-value parsing | One unit per column, from the median |
| Plausibility window 1900–2200 | Accepts old data as integers | Seconds read as nanoseconds land in 1970 and pass | Lower bound 1971 catches that case |

## Consequences
- An epoch-integer file that parsed before but produced 1970 dates now fails with a 422 and a
  hint instead of producing a nonsense report.
- The CLI keeps per-value inference; aligning it with the per-column rule and the range check
  is a follow-up in the core.
- The web upload form gains a `ts_unit` select with spec 005; until then it sends nothing and
  gets `auto`.
