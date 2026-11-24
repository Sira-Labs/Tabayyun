# Spec 034 — Core: repair operations with lineage

Sprint 11, story S11-1. Depends on: ADR-0010 (corrections are versioned layers; raw data is
immutable), spec 008 (`align`, whose bins `align.resample` shares). Packages:
`core/tabayyun-core` (module `repair`), `docs/`.

## Goal

The core can repair a window of a series: seven operations each take a raw frame and a
window and return a new frame plus a lineage frame, one row for every sample that was
changed, added or removed. The raw frame is only read, and the lineage is enough to get the
raw samples back from the repaired frame. Nothing calls the module yet; the corrections API
(S11-2) is its first user.

## User story

As a metering analyst, I fix the hour in which a meter reported zeros by estimating it from
the same hour of the neighbouring days, and I can show for every one of those values what
the meter said, what stands there now and that it is an estimate.

## Interface

`tabayyun_core::repair`:

```rust
pub fn repair(raw: &SeriesFrame, window: Window, op: &Op) -> Result<Repaired>;

pub struct Repaired { pub method: &'static str, pub frame: SeriesFrame,
                      pub lineage: Lineage, pub unrepaired: usize }

pub struct Lineage { pub ts: Vec<i64>, pub action: Vec<Action>,        // changed | added | removed
                     pub before: Vec<f64>, pub after: Vec<f64>,
                     pub quality_before: Vec<Option<Quality>>, pub quality_after: Vec<Option<Quality>> }
impl Lineage { pub fn diff(raw, repaired) -> Lineage;
               pub fn revert(&self, repaired) -> Result<SeriesFrame>;
               pub fn to_record_batch(&self) -> Result<RecordBatch>; }
```

`Op` is the operation with its parameters; in JSON the method is the tag
(`{"method": "clamp", "min": 0}`):

| Method | Parameters | What it does inside the window |
|---|---|---|
| `mask` | none | every sample becomes a null of bad quality |
| `clamp` | `min`, `max` (default: the series' physical limits) | values below `min` become `min`, above `max` become `max`, as estimated |
| `dedupe` | `keep`: `first` (default), `last`, `mean` | of the samples that share a timestamp one stays; `mean` is the mean of the usable, finite ones, as estimated |
| `impute.linear` | `max_gap_ns` (default: no limit) | holes and missing timestamps get the value on the line between the usable samples around them |
| `impute.like_day` | `days` (1 to 60, default 4), `same_weekday` (default false) | holes and missing timestamps get the mean of the usable samples at the same time of day on up to `days` days before and after |
| `align.resample` | `grid_ns` | each whole bin of the grid becomes one sample at the bin's start with the mean of its usable, finite samples |
| `transform.affine` | `scale` (default 1), `offset` (default 0) | every finite value `v` becomes `scale · v + offset`; qualities stay |

`Lineage::to_record_batch`: `ts: Timestamp(ns, UTC)`, `action: Utf8`, `before`, `after:
Float64` (null for NaN and for an absent sample), `quality_before`, `quality_after: UInt8`
(null for an absent sample).

## Behaviour

1. **Window.** The window is half-open. Samples outside it come back as they were, in their
   order; no operation writes a sample outside it.
2. **Raw is read only.** `repair` takes the raw frame by reference and returns a new frame.
   The frame must be sorted by time (samples that share a timestamp keep their order); an
   unsorted frame, a window that does not end after its start and parameters an operation
   cannot use are errors, and nothing is returned.
3. **Holes.** A sample is a hole when its value is not finite or its quality is not usable
   (bad or uncertain). Usable samples are good or estimated, as the checks count them.
4. **Missing timestamps.** With a sampling interval (the series' metadata, else the modal
   interval of the data), a timestamp is missing where two neighbouring samples are more
   than one and a half intervals apart. The imputers add the missing ones inside the window
   on the rhythm of the earlier sample. Nothing is added before the frame's first sample or
   after its last. Missing timestamps that cannot be estimated are counted as unrepaired
   without being visited; an operation that would add more than 5 000 000 samples is an
   error.
5. **Imputers do not guess.** `impute.linear` needs a usable sample on both sides (they may
   lie outside the window) and, with `max_gap_ns`, at most that far apart. `impute.like_day`
   needs the sampling interval and takes a reference within half an interval of the same
   time on another day; without any reference there is no estimate. What cannot be estimated
   stays as it is and is counted in `unrepaired`.
6. **Quality of what is written.** Imputed and clamped values and a `dedupe` mean are
   `estimated`. A resampled bin is `estimated` when one of its samples was, `good` otherwise,
   and a null of bad quality when it has samples but none usable. A single sample that
   already sits on the grid stays as it is. The affine transform keeps qualities.
7. **Whole bins only.** `align.resample` rebuilds the bins that lie wholly inside the
   window; samples in a bin the window cuts stay.
8. **Lineage.** The lineage is the difference between the raw and the repaired frame in time
   order: a sample with another value or quality is `changed`, a new one `added`, a missing
   one `removed`. Samples that are the same on both sides have no row. `revert` applied to
   the repaired frame gives the raw samples back (samples that share a timestamp in no
   particular order, and without ingest times).

## Acceptance criteria

- [x] Each of the seven operations has unit tests for its result, its lineage and its
      errors.
- [x] Property test "raw never changes": for random frames (repeated timestamps, gaps, NaN,
      every quality), windows and operations, the raw frame is untouched, the samples outside
      the window are the same, and `revert` gives the raw samples back.
- [x] Imputers write only finite, estimated values into holes and missing timestamps, never
      remove a sample, and count what they could not estimate.
- [x] Operations are read from JSON by their method name; an unknown method is an error.
- [x] The lineage converts to a record batch with the columns above.

## Test cases

Unit (`core/tabayyun-core/src/repair/tests.rs`): `mask_nulls_the_window_and_nothing_else`,
`clamp_pulls_values_to_the_bounds_as_estimated`, `dedupe_keeps_one_sample_per_timestamp`,
`impute_linear_fills_holes_and_missing_timestamps`, `impute_linear_does_not_extrapolate`,
`impute_like_day_takes_the_same_time_of_other_days`, `align_resample_rebuilds_whole_bins`,
`transform_affine_scales_and_shifts`, `lineage_reverts_and_serialises`,
`operations_read_from_json_and_name_their_method`, `invalid_frames_and_windows_are_errors`,
`an_imputer_refuses_to_add_samples_without_end`,
`a_gap_nothing_can_be_estimated_in_costs_nothing`,
`timestamps_at_the_ends_of_the_range_stay_inside_the_window`,
`many_samples_on_one_timestamp_are_matched_in_one_pass`,
`what_an_imputer_calls_fillable_it_fills`,
`a_reference_day_beyond_the_last_timestamp_is_none`.

Property (same file, 600 cases each): `raw_never_changes`, `imputers_only_estimate`,
`like_day_adds_every_timestamp_that_has_a_reference`.

## Implementation notes

- Every operation only builds the samples that replace the window's; `repair` joins them
  with the untouched samples before and after, and the lineage is computed in one place as
  the difference of the two frames. An operation therefore cannot report a lineage that
  differs from what it did.
- Where several samples share a timestamp, the difference first matches those that are the
  same on both sides and pairs the rest in order; `dedupe` with `last` so reports the
  dropped samples as removed, not the kept one as changed.
- Two lineages compare equal when their rows do; an absent value is stored as NaN and
  counts as equal to an absent value.
- A resampled bin of good samples is good, not estimated: it is the value `align` shows the
  checks for that bin. The lineage still lists every sample it replaced.
- `transform.affine` refuses a scale of 0, which would erase the values; `mask` does that
  and says so.
- The Python binding is left to S11-2, which decides what the API hands over.
- From the review:
  - The limit of 5 000 000 counts the samples that are added. Each imputer says, for a
    time in a gap, where the next timestamp is that it can estimate, and the timestamps
    before it are counted as unrepaired without being visited: `impute.linear` has either
    the whole gap or none of it, `impute.like_day` the times at which a usable sample of
    another day comes into reach. Two samples years apart cost only what can be filled.
  - A property test compares what `impute.like_day` adds with a plain search for a
    reference over all usable samples, so skipping ahead cannot miss a timestamp.
  - What an imputer calls fillable it fills: the line between two huge values of opposite
    sign and the mean of huge references are computed so that they stay numbers, and a
    reference day that would lie beyond the first or last timestamp is no reference. An
    imputer therefore never walks a gap without adding to it.
  - The missing timestamps of a gap are computed in 128-bit arithmetic, so samples at the
    two ends of the timestamp range cannot push one outside the window.
  - Samples that share a timestamp are matched through an index once there are more than
    16 of them; 40 000 on one timestamp take one pass.
  - The like-day mean is held between its smallest and largest reference: three thirds of
    the largest number round past it.

## Out of scope

- The other operations of ADR-0010 (`impute.seasonal`, `impute.kalman`, `align.shift`,
  `align.timezone`, `transform.offset`, `replace.with_reference`, `reconcile.balance`): later
  sprints; `transform.offset` is `transform.affine` with a scale of 1.
- Python binding, `Correction` and `CorrectedRange` tables, corrected layers in the cache:
  S11-2. Re-scoring: S11-3. Web: S11-4.
- Chaining operations (RepairFlows): R2.
