# ADR-0010: Corrections are versioned corrected layers with per-point lineage; raw data is immutable

- **Status:** Accepted
- **Date:** 2026-09-21

## Context
Detection without correction leaves users with a list of problems. Competing products
offer manual "select a period and clean it" plus automated cleaning flows that publish a
cleaned copy downstream. Users (metering analysts, PV/wind performance engineers, data
scientists) need to repair windows, prove what was changed and why, and hand trusted data
to downstream systems, without ever losing the original measurement.

## Decision
- Every Series has an immutable raw layer and versioned corrected layers stored beside it in
  the Parquet cache (`layer=raw | corrected/v{n}`).
- A Correction is a first-class entity with status (proposed → approved → published), method,
  parameters, actor, reason, optional originating Finding, and point-level lineage
  (`CorrectedRange`: window, before, after).
- Repair operations live in the Rust core (`core::repair`) and are pure: they return new
  values plus a lineage frame. Initial set: mask, clamp, dedupe, impute.linear,
  impute.like_day, impute.seasonal, impute.kalman, align.resample/shift/timezone,
  transform.affine/offset, replace.with_reference, reconcile.balance.
- Corrected layers are re-scored with the same checks so the quality gain is measurable.
- Corrections are published to separate destinations (Parquet/SQL export, a separate
  historian tag, API); Tabayyun never overwrites the source tag.
- RepairFlows (R2) chain operations and run on a schedule or after a suite run, under an
  approval policy (auto-approve, editor approval, dual approval).

## Alternatives considered
| Option | Pros | Cons | Why not |
|---|---|---|---|
| Overwrite the cached series | Simple | Loses provenance; irreversibility; audit failure | Unacceptable for settlement and regulated use |
| Corrections only as exported copies (no layer in the product) | Minimal storage | No diff, no re-scoring, no reversibility | Loses the feedback loop |
| Delegate repair to notebooks / Seeq-style formulas | Flexible | Ungoverned, not reproducible, no lineage | Not a product capability |

## Consequences
Cache storage grows with corrected versions (bounded by retention policy: keep latest N
versions). Chart and export endpoints take a `layer` parameter. Metering pack must map
methods to regulatory substitution codes.
