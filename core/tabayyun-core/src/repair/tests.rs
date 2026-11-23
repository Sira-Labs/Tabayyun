use super::*;
use crate::frame::SeriesMeta;
use crate::time::{NS_PER_DAY, NS_PER_HOUR, NS_PER_MIN};
use proptest::prelude::*;

const MIN: i64 = NS_PER_MIN;
const G: Quality = Quality::Good;
const E: Quality = Quality::Estimated;
const B: Quality = Quality::Bad;
const NAN: f64 = f64::NAN;

fn frame(values: &[f64]) -> SeriesFrame {
    let ts = (0..values.len() as i64).map(|i| i * MIN).collect();
    SeriesFrame::with_default_quality(SeriesMeta::new("s"), ts, values.to_vec()).unwrap()
}

fn at(ts: &[i64], values: &[f64]) -> SeriesFrame {
    SeriesFrame::with_default_quality(
        SeriesMeta::new("s"),
        ts.iter().map(|t| t * MIN).collect(),
        values.to_vec(),
    )
    .unwrap()
}

fn w(start_min: i64, end_min: i64) -> Window {
    Window::new(start_min * MIN, end_min * MIN)
}

/// Values with NaN written as a marker, so slices compare with `assert_eq`.
fn shown(values: &[f64]) -> Vec<String> {
    values.iter().map(|v| if v.is_nan() { "nan".into() } else { format!("{v}") }).collect()
}

/// A frame's samples in an order that does not depend on how equal timestamps are arranged.
fn canonical(f: &SeriesFrame) -> Vec<(i64, u64, u8)> {
    let bits = |v: f64| if v.is_nan() { u64::MAX } else { (v + 0.0).to_bits() };
    let mut rows: Vec<_> = (0..f.len()).map(|i| (f.ts[i], bits(f.values[i]), f.quality[i].as_u8())).collect();
    rows.sort_unstable();
    rows
}

fn rows(l: &Lineage) -> Vec<(i64, Action, String, String)> {
    (0..l.len())
        .map(|i| {
            (l.ts[i] / MIN, l.action[i], shown(&[l.before[i]])[0].clone(), shown(&[l.after[i]])[0].clone())
        })
        .collect()
}

#[test]
fn mask_nulls_the_window_and_nothing_else() {
    let raw = frame(&[1.0, 2.0, 3.0, 4.0, 5.0]);
    let out = repair(&raw, w(1, 3), &Op::Mask).unwrap();
    assert_eq!(out.method, "mask");
    assert_eq!(shown(&out.frame.values), shown(&[1.0, NAN, NAN, 4.0, 5.0]));
    assert_eq!(out.frame.quality, vec![G, B, B, G, G]);
    assert_eq!(out.frame.ts, raw.ts);
    assert_eq!(
        rows(&out.lineage),
        vec![(1, Action::Changed, "2".into(), "nan".into()), (2, Action::Changed, "3".into(), "nan".into())]
    );
    assert_eq!(out.lineage.quality_before, vec![Some(G), Some(G)]);
    assert_eq!(out.lineage.quality_after, vec![Some(B), Some(B)]);
    // Masking what is masked already changes nothing.
    let again = repair(&out.frame, w(1, 3), &Op::Mask).unwrap();
    assert!(again.lineage.is_empty() && canonical(&again.frame) == canonical(&out.frame));
}

#[test]
fn clamp_pulls_values_to_the_bounds_as_estimated() {
    let raw = frame(&[-5.0, 50.0, 150.0, NAN, 99.0, 500.0]);
    let op = Op::Clamp { min: Some(0.0), max: Some(100.0) };
    let out = repair(&raw, w(0, 5), &op).unwrap();
    // The last sample is outside the window; the NaN stays a NaN.
    assert_eq!(shown(&out.frame.values), shown(&[0.0, 50.0, 100.0, NAN, 99.0, 500.0]));
    assert_eq!(out.frame.quality, vec![E, G, E, G, G, G]);
    assert_eq!(out.lineage.len(), 2);
    // Without bounds the series' physical limits are used; one bound alone works too.
    let mut percent = raw.clone();
    percent.meta.unit = Some("%".into());
    let by_unit = repair(&percent, w(0, 6), &Op::Clamp { min: None, max: None }).unwrap();
    assert_eq!(shown(&by_unit.frame.values), shown(&[0.0, 50.0, 100.0, NAN, 99.0, 100.0]));
    let upper = repair(&raw, w(0, 6), &Op::Clamp { min: None, max: Some(100.0) }).unwrap();
    assert_eq!(shown(&upper.frame.values), shown(&[-5.0, 50.0, 100.0, NAN, 99.0, 100.0]));
    assert!(repair(&raw, w(0, 6), &Op::Clamp { min: None, max: None }).is_err());
    assert!(repair(&raw, w(0, 6), &Op::Clamp { min: Some(2.0), max: Some(1.0) }).is_err());
    assert!(repair(&raw, w(0, 6), &Op::Clamp { min: Some(NAN), max: None }).is_err());
}

#[test]
fn dedupe_keeps_one_sample_per_timestamp() {
    let mut raw = at(&[0, 1, 1, 1, 2, 3, 3, 4, 4], &[1.0, 10.0, 20.0, 60.0, 2.0, 7.0, 7.0, NAN, 5.0]);
    raw.quality[7] = B;
    let run = |keep| repair(&raw, w(0, 5), &Op::Dedupe { keep }).unwrap();
    let first = run(Keep::First);
    assert_eq!(first.frame.ts, [0, 1, 2, 3, 4].map(|t| t * MIN));
    assert_eq!(shown(&first.frame.values), shown(&[1.0, 10.0, 2.0, 7.0, NAN]));
    assert_eq!(first.frame.quality, vec![G, G, G, G, B]);
    assert_eq!((first.lineage.len(), first.lineage.count(Action::Removed)), (4, 4));
    let last = run(Keep::Last);
    assert_eq!(shown(&last.frame.values), shown(&[1.0, 60.0, 2.0, 7.0, 5.0]));
    assert_eq!(last.frame.quality, vec![G; 5]);
    // The mean is over the usable samples and is an estimate; equal samples stay as they are.
    let mean = run(Keep::Mean);
    assert_eq!(shown(&mean.frame.values), shown(&[1.0, 30.0, 2.0, 7.0, 5.0]));
    assert_eq!(mean.frame.quality, vec![G, E, G, G, E]);
    assert_eq!(
        rows(&mean.lineage)[..3],
        [
            (1, Action::Changed, "10".to_string(), "30".to_string()),
            (1, Action::Removed, "20".to_string(), "nan".to_string()),
            (1, Action::Removed, "60".to_string(), "nan".to_string()),
        ]
    );
    // Duplicates outside the window stay.
    let part = repair(&raw, w(0, 2), &Op::Dedupe { keep: Keep::First }).unwrap();
    assert_eq!(part.frame.len(), 7);
}

#[test]
fn impute_linear_fills_holes_and_missing_timestamps() {
    // Minute 2 is a NaN, minute 3 is bad, minutes 5 and 6 are missing.
    let mut raw = at(&[0, 1, 2, 3, 4, 7, 8], &[0.0, 10.0, NAN, 999.0, 40.0, 70.0, 80.0]);
    raw.quality[3] = B;
    let out = repair(&raw, w(0, 9), &Op::ImputeLinear { max_gap_ns: None }).unwrap();
    assert_eq!(out.frame.ts, (0..9).map(|t| t * MIN).collect::<Vec<_>>());
    assert_eq!(out.frame.values, vec![0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0]);
    assert_eq!(out.frame.quality, vec![G, G, E, E, G, E, E, G, G]);
    assert_eq!(out.unrepaired, 0);
    assert_eq!((out.lineage.count(Action::Changed), out.lineage.count(Action::Added)), (2, 2));
    assert_eq!(out.lineage.quality_before, vec![Some(G), Some(B), None, None]);
    // Only the window is filled, with neighbours from outside it.
    let part = repair(&raw, w(5, 6), &Op::ImputeLinear { max_gap_ns: None }).unwrap();
    assert_eq!(rows(&part.lineage), vec![(5, Action::Added, "nan".into(), "50".into())]);
    // A gap above the limit stays as it is and is counted.
    let short = repair(&raw, w(0, 9), &Op::ImputeLinear { max_gap_ns: Some(2 * MIN) }).unwrap();
    assert_eq!(short.unrepaired, 4);
    assert!(short.lineage.is_empty());
    let enough = repair(&raw, w(0, 9), &Op::ImputeLinear { max_gap_ns: Some(3 * MIN) }).unwrap();
    assert_eq!((enough.unrepaired, enough.lineage.len()), (0, 4));
    assert!(repair(&raw, w(0, 9), &Op::ImputeLinear { max_gap_ns: Some(0) }).is_err());
}

#[test]
fn impute_linear_does_not_extrapolate() {
    // Holes at both ends have a usable sample on one side only.
    let raw = frame(&[NAN, NAN, 3.0, 4.0, NAN]);
    let out = repair(&raw, w(0, 5), &Op::ImputeLinear { max_gap_ns: None }).unwrap();
    assert_eq!(shown(&out.frame.values), shown(&raw.values));
    assert_eq!((out.unrepaired, out.lineage.len()), (3, 0));
    // A window before or after the data adds nothing.
    let outside = repair(&raw, w(10, 20), &Op::ImputeLinear { max_gap_ns: None }).unwrap();
    assert!(outside.lineage.is_empty() && outside.frame.len() == 5 && outside.unrepaired == 0);
    // Without a sampling interval only existing holes are filled.
    let two = at(&[0, 9], &[1.0, 10.0]);
    assert!(repair(&two, w(0, 10), &Op::ImputeLinear { max_gap_ns: None }).unwrap().lineage.is_empty());
}

#[test]
fn impute_like_day_takes_the_same_time_of_other_days() {
    // Hourly values for five days: the hour of day plus ten per day. Day 2 misses hours 5 to 7
    // (hour 6 as a bad sample, the others not there at all).
    let hour = NS_PER_HOUR;
    let mut ts = Vec::new();
    let mut values = Vec::new();
    for h in 0..5 * 24 {
        let (day, of_day) = (h / 24, h % 24);
        if day == 2 && (of_day == 5 || of_day == 7) {
            continue;
        }
        ts.push(h * hour);
        values.push(of_day as f64 + 10.0 * day as f64);
    }
    let mut raw = SeriesFrame::with_default_quality(SeriesMeta::new("s"), ts, values).unwrap();
    let bad = raw.ts.iter().position(|&t| t == (2 * 24 + 6) * hour).unwrap();
    raw.quality[bad] = B;
    let window = Window::new(2 * NS_PER_DAY, 3 * NS_PER_DAY);
    let out = repair(&raw, window, &Op::ImputeLikeDay { days: 1, same_weekday: false }).unwrap();
    // The mean of the day before (10 + hour) and the day after (30 + hour).
    assert_eq!(
        rows(&out.lineage).iter().map(|r| (r.0 / 60 - 48, r.1, r.3.clone())).collect::<Vec<_>>(),
        vec![
            (5, Action::Added, "25".to_string()),
            (6, Action::Changed, "26".to_string()),
            (7, Action::Added, "27".to_string())
        ]
    );
    assert_eq!(out.frame.len(), 5 * 24);
    assert!(out.lineage.quality_after.iter().all(|q| *q == Some(E)));
    // Two days each way: days 0, 1, 3 and 4 give the same mean here.
    let wide = repair(&raw, window, &Op::ImputeLikeDay { days: 2, same_weekday: false }).unwrap();
    assert_eq!(wide.lineage.after, vec![25.0, 26.0, 27.0]);
    // The same weekday is a week away, where this series has nothing.
    let weekly = repair(&raw, window, &Op::ImputeLikeDay { days: 4, same_weekday: true }).unwrap();
    assert_eq!((weekly.unrepaired, weekly.lineage.len()), (3, 0));
    assert!(repair(&raw, window, &Op::ImputeLikeDay { days: 0, same_weekday: false }).is_err());
    let lone = at(&[0], &[NAN]);
    assert!(repair(&lone, w(0, 1), &Op::ImputeLikeDay { days: 1, same_weekday: false }).is_err());
}

#[test]
fn align_resample_rebuilds_whole_bins() {
    // Samples at odd seconds, on a five-minute grid; the window covers minutes 5 to 20.
    let s = NS_PER_MIN / 60;
    let ts = vec![0, 4 * MIN, 5 * MIN + 7 * s, 7 * MIN, 9 * MIN, 10 * MIN, 16 * MIN, 17 * MIN, 21 * MIN];
    let values = vec![1.0, 2.0, 10.0, 20.0, 30.0, 5.0, 8.0, NAN, 9.0];
    let mut raw = SeriesFrame::with_default_quality(SeriesMeta::new("s"), ts, values).unwrap();
    raw.quality[6] = B;
    let out = repair(&raw, w(5, 20), &Op::AlignResample { grid_ns: 5 * MIN }).unwrap();
    assert_eq!(out.frame.ts, [0, 4, 5, 10, 15, 21].map(|t| t * MIN));
    // Bin 5: the mean of three; bin 10: one sample on the grid, kept; bin 15: nothing usable.
    assert_eq!(shown(&out.frame.values), shown(&[1.0, 2.0, 20.0, 5.0, NAN, 9.0]));
    assert_eq!(out.frame.quality, vec![G, G, G, G, B, G]);
    assert_eq!((out.lineage.count(Action::Removed), out.lineage.count(Action::Added)), (5, 2));
    // A window that cuts a bin leaves that bin's samples alone.
    let cut = repair(&raw, w(6, 20), &Op::AlignResample { grid_ns: 5 * MIN }).unwrap();
    assert_eq!(cut.frame.ts[2..6], [5 * MIN + 7 * s, 7 * MIN, 9 * MIN, 10 * MIN]);
    // An estimated sample makes its bin estimated.
    raw.quality[3] = E;
    let est = repair(&raw, w(5, 20), &Op::AlignResample { grid_ns: 5 * MIN }).unwrap();
    assert_eq!(est.frame.quality[2], E);
    assert!(repair(&raw, w(5, 20), &Op::AlignResample { grid_ns: 0 }).is_err());
}

#[test]
fn transform_affine_scales_and_shifts() {
    let mut raw = frame(&[1.0, 2.0, NAN, 4.0]);
    raw.quality[1] = Quality::Uncertain;
    let out = repair(&raw, w(1, 4), &Op::TransformAffine { scale: 1000.0, offset: -5.0 }).unwrap();
    assert_eq!(shown(&out.frame.values), shown(&[1.0, 1995.0, NAN, 3995.0]));
    assert_eq!(out.frame.quality, raw.quality);
    assert_eq!(out.lineage.len(), 2);
    let same = repair(&raw, w(0, 4), &Op::TransformAffine { scale: 1.0, offset: 0.0 }).unwrap();
    assert!(same.lineage.is_empty());
    for (scale, offset) in [(0.0, 1.0), (NAN, 0.0), (1.0, f64::INFINITY)] {
        assert!(repair(&raw, w(0, 4), &Op::TransformAffine { scale, offset }).is_err());
    }
}

#[test]
fn lineage_reverts_and_serialises() {
    let raw = at(&[0, 1, 1, 2, 5], &[1.0, 2.0, 3.0, NAN, 9.0]);
    let out = repair(&raw, w(0, 6), &Op::ImputeLinear { max_gap_ns: None }).unwrap();
    assert_eq!(canonical(&out.lineage.revert(&out.frame).unwrap()), canonical(&raw));
    let batch = out.lineage.to_record_batch().unwrap();
    assert_eq!(batch.num_rows(), out.lineage.len());
    let names: Vec<_> = batch.schema().fields().iter().map(|f| f.name().clone()).collect();
    assert_eq!(names, ["ts", "action", "before", "after", "quality_before", "quality_after"]);
    // An added sample has no value and no quality before.
    let added = out.lineage.action.iter().position(|a| *a == Action::Added).unwrap();
    assert!(
        batch.column(2).is_null(added) && batch.column(4).is_null(added) && !batch.column(5).is_null(added)
    );
    // A lineage that does not belong to the frame is refused.
    assert!(out.lineage.revert(&raw).is_err());
    assert_eq!(Lineage::diff(&raw, &raw), Lineage::default());
}

#[test]
fn operations_read_from_json_and_name_their_method() {
    let ops: Vec<Op> = serde_json::from_str(
        r#"[{"method": "mask"}, {"method": "clamp", "max": 5}, {"method": "dedupe"},
            {"method": "impute.linear"}, {"method": "impute.like_day"},
            {"method": "align.resample", "grid_ns": 60}, {"method": "transform.affine", "offset": 2}]"#,
    )
    .unwrap();
    assert_eq!(ops.iter().map(Op::method).collect::<Vec<_>>(), METHODS);
    assert_eq!(ops[1], Op::Clamp { min: None, max: Some(5.0) });
    assert_eq!(ops[2], Op::Dedupe { keep: Keep::First });
    assert_eq!(ops[4], Op::ImputeLikeDay { days: 4, same_weekday: false });
    assert_eq!(ops[6], Op::TransformAffine { scale: 1.0, offset: 2.0 });
    assert_eq!(serde_json::to_string(&ops[3]).unwrap(), r#"{"method":"impute.linear","max_gap_ns":null}"#);
    assert!(serde_json::from_str::<Op>(r#"{"method": "impute.kalman"}"#).is_err());
    assert!(serde_json::from_str::<Op>(r#"{"method": "align.resample"}"#).is_err());
}

#[test]
fn invalid_frames_and_windows_are_errors() {
    let raw = frame(&[1.0, 2.0]);
    assert!(repair(&raw, w(1, 1), &Op::Mask).is_err());
    assert!(repair(&raw, w(2, 1), &Op::Mask).is_err());
    let unsorted = at(&[1, 0], &[1.0, 2.0]);
    assert!(repair(&unsorted, w(0, 2), &Op::Mask).is_err());
    // An empty frame and a window without samples are fine.
    let empty = frame(&[]);
    assert!(repair(&empty, w(0, 5), &Op::Mask).unwrap().lineage.is_empty());
    assert!(repair(&raw, w(5, 9), &Op::Mask).unwrap().lineage.is_empty());
}

#[test]
fn an_imputer_refuses_to_add_samples_without_end() {
    // Two samples a century apart on a one-second rhythm.
    let mut raw = at(&[0, 1, 2], &[1.0, 2.0, 3.0]);
    raw.ts.push(100 * 365 * NS_PER_DAY);
    raw.values.push(4.0);
    raw.quality.push(G);
    raw.meta.expected_interval_ns = Some(1_000_000_000);
    let all = Window::new(0, i64::MAX);
    assert!(repair(&raw, all, &Op::ImputeLinear { max_gap_ns: None }).is_err());
}

#[test]
fn a_gap_nothing_can_be_estimated_in_costs_nothing() {
    // Two samples 6 000 001 seconds apart on a one-second rhythm, and a limit of one second:
    // nothing can be added, so the frame comes back with every missing second counted.
    let second = 1_000_000_000;
    let mut raw =
        SeriesFrame::with_default_quality(SeriesMeta::new("s"), vec![0, 6_000_001 * second], vec![1.0, 2.0])
            .unwrap();
    raw.meta.expected_interval_ns = Some(second);
    let all = Window::new(0, i64::MAX);
    let out = repair(&raw, all, &Op::ImputeLinear { max_gap_ns: Some(second) }).unwrap();
    assert_eq!((out.unrepaired, out.lineage.len(), out.frame.len()), (6_000_000, 0, 2));
    // Like-day with 60 days each way finds a reference for 120 of those seconds only: the
    // same second 1 to 60 days after the first sample and before the last.
    let out = repair(&raw, all, &Op::ImputeLikeDay { days: 60, same_weekday: false }).unwrap();
    assert_eq!((out.lineage.len(), out.unrepaired), (120, 6_000_000 - 120));
    assert_eq!(out.lineage.ts[0], NS_PER_DAY);
    assert_eq!(out.lineage.after[0], 1.0);
    assert_eq!(*out.lineage.ts.last().unwrap(), 6_000_001 * second - NS_PER_DAY);
    // Like-day reaches one day from each end of a gap of 400 days; the middle is skipped.
    let minute = MIN;
    let before: Vec<i64> = (0..2 * 24 * 60).map(|i| i * minute).collect();
    let after: Vec<i64> = (0..2 * 24 * 60).map(|i| 402 * NS_PER_DAY + i * minute).collect();
    let ts: Vec<i64> = before.iter().chain(&after).copied().collect();
    let values = vec![5.0; ts.len()];
    let raw = SeriesFrame::with_default_quality(SeriesMeta::new("s"), ts, values).unwrap();
    let out = repair(&raw, all, &Op::ImputeLikeDay { days: 1, same_weekday: false }).unwrap();
    let missing = (400 * 24 * 60 + 1 - 1) as usize;
    assert_eq!(out.unrepaired + out.lineage.len(), missing);
    // One day at each end has the day beside it to take from.
    assert_eq!(out.lineage.len(), 2 * 24 * 60);
    assert!(out.lineage.after.iter().all(|v| *v == 5.0));
}

#[test]
fn timestamps_at_the_ends_of_the_range_stay_inside_the_window() {
    // The samples are the whole range of i64 apart; the first missing timestamp inside the
    // window must not be computed with arithmetic that saturates.
    let mut raw =
        SeriesFrame::with_default_quality(SeriesMeta::new("s"), vec![i64::MIN, i64::MAX], vec![0.0, 1.0])
            .unwrap();
    raw.meta.expected_interval_ns = Some(4);
    let out = repair(&raw, Window::new(0, 4), &Op::ImputeLinear { max_gap_ns: None }).unwrap();
    assert_eq!((out.lineage.ts.clone(), out.lineage.count(Action::Added)), (vec![0], 1));
    assert!((out.lineage.after[0] - 0.5).abs() < 1e-9);
    let out = repair(&raw, Window::new(-9, 9), &Op::ImputeLinear { max_gap_ns: None }).unwrap();
    assert_eq!(out.lineage.ts, vec![-8, -4, 0, 4, 8]);
}

#[test]
fn many_samples_on_one_timestamp_are_matched_in_one_pass() {
    // 40 000 samples share a timestamp, with NaN and both zeros among them.
    let n = 40_000;
    let mut values: Vec<f64> = (0..n).map(|i| f64::from(i % 1000)).collect();
    values[7] = NAN;
    values[8] = -0.0;
    let raw = SeriesFrame::with_default_quality(SeriesMeta::new("s"), vec![MIN; n as usize], values).unwrap();
    let out = repair(&raw, w(0, 5), &Op::Dedupe { keep: Keep::Last }).unwrap();
    assert_eq!((out.frame.len(), out.lineage.count(Action::Removed)), (1, n as usize - 1));
    assert_eq!(canonical(&out.lineage.revert(&out.frame).unwrap()), canonical(&raw));
    // Every sample changes: nothing matches, and the pairs keep the order of the frame.
    let scaled = repair(&raw, w(0, 5), &Op::TransformAffine { scale: 1.0, offset: 0.5 }).unwrap();
    assert_eq!(scaled.lineage.count(Action::Changed), n as usize - 1);
    assert_eq!((scaled.lineage.before[0], scaled.lineage.after[0]), (0.0, 0.5));
    assert_eq!(canonical(&scaled.lineage.revert(&scaled.frame).unwrap()), canonical(&raw));
}

fn any_op() -> impl Strategy<Value = Op> {
    prop_oneof![
        Just(Op::Mask),
        (-50.0..50.0f64, 0.0..60.0f64)
            .prop_map(|(min, width)| Op::Clamp { min: Some(min), max: Some(min + width) }),
        prop_oneof![Just(Keep::First), Just(Keep::Last), Just(Keep::Mean)]
            .prop_map(|keep| Op::Dedupe { keep }),
        proptest::option::of(1..20i64).prop_map(|g| Op::ImputeLinear { max_gap_ns: g.map(|m| m * MIN) }),
        (1..4usize, any::<bool>()).prop_map(|(days, same_weekday)| Op::ImputeLikeDay { days, same_weekday }),
        (1..10i64).prop_map(|m| Op::AlignResample { grid_ns: m * MIN }),
        (-3.0..3.0f64, -10.0..10.0f64).prop_map(|(scale, offset)| Op::TransformAffine {
            scale: if scale == 0.0 { 1.0 } else { scale },
            offset
        }),
    ]
}

/// A sorted frame on a one-minute rhythm with repeated timestamps, gaps, NaN and every quality.
fn any_frame() -> impl Strategy<Value = SeriesFrame> {
    let step = prop_oneof![4 => Just(1i64), 1 => Just(0i64), 1 => 2..6i64, 1 => Just(24 * 60i64)];
    let value = prop_oneof![8 => -100.0..100.0f64, 1 => Just(f64::NAN), 1 => Just(0.0)];
    let quality = prop_oneof![6 => Just(G), 1 => Just(Quality::Uncertain), 1 => Just(B), 1 => Just(E)];
    proptest::collection::vec((step, value, quality), 0..120).prop_map(|samples| {
        let mut t = 0i64;
        let (mut ts, mut values, mut qualities) = (Vec::new(), Vec::new(), Vec::new());
        for (step, value, quality) in samples {
            t += step * MIN;
            ts.push(t);
            values.push(value);
            qualities.push(quality);
        }
        let mut meta = SeriesMeta::new("s");
        meta.expected_interval_ns = Some(MIN);
        SeriesFrame::new(meta, ts, values, qualities).unwrap()
    })
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(600))]

    /// Raw never changes: the frame handed in is as it was, the samples outside the window
    /// come back untouched, and the lineage gives the raw samples back from the repaired frame.
    #[test]
    fn raw_never_changes(raw in any_frame(), op in any_op(), start in -5..400i64, length in 1..3000i64) {
        let window = w(start, start + length);
        let kept = raw.clone();
        let out = repair(&raw, window, &op).unwrap();
        prop_assert_eq!(canonical(&raw), canonical(&kept));
        prop_assert_eq!(&raw.ts, &kept.ts);

        prop_assert!(out.frame.is_sorted());
        prop_assert_eq!(canonical(&out.lineage.revert(&out.frame).unwrap()), canonical(&raw));
        prop_assert_eq!(&Lineage::diff(&raw, &out.frame), &out.lineage);
        prop_assert!(out.lineage.ts.iter().all(|t| (window.start..window.end).contains(t)));

        let outside = |f: &SeriesFrame| -> Vec<(i64, u64, u8)> {
            canonical(f).into_iter().filter(|r| !(window.start..window.end).contains(&r.0)).collect()
        };
        prop_assert_eq!(outside(&out.frame), outside(&raw));
        if out.lineage.is_empty() {
            prop_assert_eq!(canonical(&out.frame), canonical(&raw));
        }
    }

    /// Skipping ahead to what can be estimated misses nothing: like-day adds exactly the
    /// missing timestamps for which a plain search over all usable samples finds a reference.
    #[test]
    fn like_day_adds_every_timestamp_that_has_a_reference(raw in any_frame(), days in 1..4usize) {
        let all = Window::new(-MIN, i64::MAX);
        let out = repair(&raw, all, &Op::ImputeLikeDay { days, same_weekday: false }).unwrap();
        let known: Vec<i64> = (0..raw.len()).filter(|&i| usable(raw.values[i], raw.quality[i])).map(|i| raw.ts[i]).collect();
        let has_reference = |t: i64| {
            (1..=days as i64).any(|k| known.iter().any(|u| (u - (t - k * NS_PER_DAY)).abs() <= MIN / 2 || (u - (t + k * NS_PER_DAY)).abs() <= MIN / 2))
        };
        let mut expected = Vec::new();
        let mut missing = 0usize;
        for pair in raw.ts.windows(2) {
            let mut t = pair[0] + MIN;
            while pair[1] - pair[0] > MIN + MIN / 2 && pair[1] - t > MIN / 2 {
                missing += 1;
                if has_reference(t) {
                    expected.push(t);
                }
                t += MIN;
            }
        }
        let added: Vec<i64> = (0..out.lineage.len()).filter(|&i| out.lineage.action[i] == Action::Added).map(|i| out.lineage.ts[i]).collect();
        prop_assert_eq!(&added, &expected);
        let holes = (0..raw.len()).filter(|&i| !usable(raw.values[i], raw.quality[i])).count();
        prop_assert_eq!(out.unrepaired, missing - expected.len() + holes - out.lineage.count(Action::Changed));
    }

    /// What an imputer writes is finite and estimated, and it never removes a sample.
    #[test]
    fn imputers_only_estimate(raw in any_frame(), like_day in any::<bool>(), start in -5..400i64, length in 1..3000i64) {
        let op = if like_day { Op::ImputeLikeDay { days: 2, same_weekday: false } } else { Op::ImputeLinear { max_gap_ns: None } };
        let out = repair(&raw, w(start, start + length), &op).unwrap();
        prop_assert_eq!(out.lineage.count(Action::Removed), 0);
        for i in 0..out.lineage.len() {
            prop_assert!(out.lineage.after[i].is_finite());
            prop_assert_eq!(out.lineage.quality_after[i], Some(E));
            prop_assert!(out.lineage.quality_before[i].is_none_or(|q| !usable(out.lineage.before[i], q)));
        }
    }
}
