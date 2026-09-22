# ADR-0013: Run and finding lifecycle, deduplication across runs

- **Status:** Accepted
- **Date:** 2026-09-22
- **Deciders:** core maintainers

## Context
Spec 002 made a run a persistent, asynchronous object; spec 003 stores what a run finds.
Users upload the same month twice, upload overlapping files, and re-run checks after a
fix. Stored naively, every run duplicates every finding and a triage list grows with the
number of runs instead of the number of problems. ADR-0011 already made a finding an
*episode* within one run; this ADR decides how episodes from different runs relate, what a
user can do with a finding, and what a run leaves behind when it fails.

One check can emit several overlapping findings in one run that are different statements:
`tby.completeness` reports each gap and one whole-window finding whose window spans every
gap. A rule that merges "any overlapping finding of the same check" would fold the gaps
into the whole-window finding and lose them.

## Decision
1. **Run states.** `queued → running → succeeded | failed`. Only a run still `running`
   completes (one guarded update; the stale-run reaper may have failed it meanwhile). The
   score, metric points, findings and run statistics are written in that one completion
   transaction: a run that fails persists nothing but its error. The uploaded bytes are
   deleted once the run is terminal.
2. **Finding states.** `open`, `acked`, `muted`, `resolved`. Allowed moves:
   `open → acked | muted | resolved`, `acked → resolved | open`, `muted → open`,
   `resolved → open`. Muting needs a reason (≤ 500 characters); the other moves take one
   optionally. Any other move is a conflict (HTTP 409 with the current status). Every move
   sets `status_at`; `status_by` is filled once authentication exists (spec 007). Findings
   are never deleted: a wrong finding is resolved with a reason.
3. **Deduplication across runs.** An incoming finding merges into an existing finding when
   all of these hold:
   - same series and same `check_id`;
   - the existing finding is `open` or `acked`;
   - the same *evidence shape*, i.e. the same set of top-level evidence keys, which tells
     the kinds of finding one check emits apart (a gap from the whole-window completeness
     finding, a spike cluster from the recurring-spikes summary);
   - it was created by an earlier run (findings of one run are distinct statements by
     ADR-0011 and never merge with each other);
   - the windows overlap (half-open, `start < other.end and other.start < end`).

   Among several candidates the one with the largest overlap ratio (intersection over union)
   wins; ties go to the earliest window. The merge sets the window to the union, takes
   severity, score impact, summary and evidence from the incoming finding, sets
   `last_run_id`, and increments `occurrences` once per run (two incoming findings of one
   run that merge into the same finding count once). An `acked` finding stays `acked`.
   Otherwise the incoming finding is inserted `open` with `first_run_id = last_run_id`.
4. **Muted and resolved findings never absorb.** A problem re-reported after it was resolved
   is a new finding, so the resolution and its reason remain on record.
5. **Concurrency.** Deduplication for one series runs under a transaction-scoped advisory
   lock keyed by the series, so two runs of the same series merge one after the other.
6. **Storage precision.** Windows are `timestamptz` (microseconds). Starts round down and
   ends round up, so a stored window always contains the core's `[start, end)`. A window that
   grows backwards is rewritten as delete + insert under the same id, because `window_start`
   is part of the primary key and the hypertable's partition column. Metric points are keyed
   by `(series, check, name, ts)`; a rerun over the same data rewrites them.

## Alternatives considered
| Option | Pros | Cons | Why not |
|---|---|---|---|
| No deduplication, one finding per run | Simplest; exact history | Triage list grows with runs; "seen in 2 runs" needs a query over all findings | Defeats the user story |
| Fingerprint = hash of check, series and window | Cheap exact matching | Any shift of the window (a longer file, a later `now`) breaks the match | Overlap is the stable signal |
| Merge any overlapping finding of the same check | Matches the spec's first wording | Folds each gap into the whole-window completeness finding | Loses findings; replaced by the evidence-shape rule |
| Explicit `kind` field emitted by the core | Precise | Needs a core and evidence-schema change for every check | Evidence keys already carry the kind; revisit if a check varies its keys by case |
| Resolved findings absorb re-detections and reopen | Fewer rows | Erases the resolution and its reason; hides regressions | A regression is news |
| Auto-resolve when a covering run no longer reports the problem | Keeps the list current | Needs coverage knowledge per check (suites, sprint 10) | Deferred |

## Consequences
- Uploading the same data again yields the same findings with `occurrences` incremented,
  so the list reflects problems, not runs. Run statistics report `n_findings_new` and
  `n_findings_merged`.
- Evidence shapes are part of the contract: a check that adds a key to its evidence starts a
  new line of findings for open problems once. Checks must keep the key set of one kind of
  finding stable; optional facts go inside a nested object.
- `GET /api/findings?run_id=` matches the first or the last run of a finding; a per-run link
  table can replace this if a finding's full run history is needed.
- Follow-ups: auto-resolution with suites (sprint 10), `status_by` and audit events with
  authentication (spec 007), assignment and comments with the findings inbox (sprint 10).
