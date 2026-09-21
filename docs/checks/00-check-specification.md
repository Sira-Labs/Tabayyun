# Check specification

Every check, built-in or plugin, is described by the same manifest and produces the same
outputs. The engine does not care whether the implementation is Rust or Python.

## Manifest (`check.yaml`)

```yaml
id: tby.stale_data              # namespace.name; "tby" is reserved for built-ins
version: 1                      # bump when semantics or default thresholds change
title: Stale data
dimension: timeliness           # completeness | timeliness | validity | accuracy | consistency | plausibility | integrity
category: generic               # generic | energy.pv | energy.wind | energy.grid | energy.metering | energy.storage
applies_to:
  kinds: [measurement, counter] # series kinds this check is meaningful for
  dtypes: [float, int]
  requires_metadata: []         # e.g. [expected_interval, physical_max, unit]
  requires_related: []          # e.g. [reference_series, redundant_series]
parameters:                     # JSON-schema fragment; defaults are the product opinion
  max_age:
    type: duration
    default: "auto"             # "auto" = 3 × expected_interval, min 5m
    description: Maximum age of the newest good sample before the series is stale.
  ignore_quality_bad:
    type: boolean
    default: true
severity_default: high
evidence_schema:                # what the implementation must return per finding
  newest_ts: timestamp
  age: duration
  expected_interval: duration
explain: |
  Human-readable template with {placeholders} filled from evidence, e.g.
  "Newest good sample is {age} old (expected every {expected_interval})."
docs: docs/checks/catalogue.md#stale-data
implementation:
  kind: rust                    # rust | python
  entrypoint: tabayyun_core::checks::stale_data
```

## Execution contract

```
run(series: SeriesFrame, related: {name -> SeriesFrame}, params, baseline?) -> CheckResult
```

- `SeriesFrame` is an Arrow record batch: `ts: timestamp[ns, tz]`, `value: f64|i64|bool|str`,
  `quality: u16` (OPC-style: good/uncertain/bad + substatus), plus series metadata.
- `baseline` is an optional profile computed on a reference window (for drift-type checks).
- `CheckResult` = list of `Finding{window, severity, score_impact, evidence}` + optional
  `metrics` (named scalars per window, kept as a time series for trending, e.g. `stale_age`).
- Checks must be **pure** (no I/O) and **idempotent**. The engine handles fetching, caching,
  windowing, parallelism and persistence.

## Threshold strategy

1. **Metadata first.** If the series has `physical_min/max`, `expected_interval`, unit, use them.
2. **Auto-baseline second.** Otherwise derive robust defaults from a training window
   (median, MAD, quantiles, dominant sampling interval) and store them with the run so the
   user can see and override them.
3. **Explicit override last.** Users may pin parameters per series, per dataset or per workspace.

## Severity and score impact

| Severity | Weight | Meaning |
|---|---|---|
| critical | 1.0 | Data is unusable in the affected window (e.g. wrong unit, frozen for days). |
| high | 0.6 | Most consumers should not use the window without treatment. |
| medium | 0.3 | Needs attention; downstream may tolerate. |
| low | 0.1 | Informational; affects long-term trend rather than immediate use. |

`score_impact = weight × affected_fraction`, where `affected_fraction` is the share of the
evaluation window covered by findings of this check (capped at 1).
