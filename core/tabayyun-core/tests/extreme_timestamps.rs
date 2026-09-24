//! Timestamps are any `i64`: frames built from typed Arrow columns or the bindings are not
//! range-checked, so every check, the profile and the cross checks must survive instants near
//! the `i64` limits. Debug builds panic on overflow, so this test fails on any unchecked
//! timestamp arithmetic; release builds would wrap silently into nonsense windows.

use std::collections::BTreeMap;

use tabayyun_core::time::NS_PER_MIN;
use tabayyun_core::{
    CheckConfig, CheckContext, GroupKind, GroupMember, MemberRole, Profile, Quality, Registry, Scorer,
    SeriesFrame, SeriesGroup, SeriesMeta, Window,
};

/// Timestamp layouts that overflow naive differences, gap cuts or segment arithmetic.
fn layouts() -> Vec<(&'static str, Vec<i64>)> {
    let minutes = |n: i64| (0..n).map(|i| i * NS_PER_MIN).collect::<Vec<_>>();
    vec![
        ("both_limits", {
            let mut ts = minutes(300);
            ts[0] = i64::MIN;
            ts.push(i64::MAX);
            ts
        }),
        ("huge_steps", (0..26).map(|i| i64::MIN + 1 + i * (i64::MAX / 25)).collect()),
        ("near_max", (0..300).map(|i| i64::MAX - 300 * NS_PER_MIN + i * NS_PER_MIN).collect()),
        ("near_min", (0..300).map(|i| i64::MIN + i * NS_PER_MIN).collect()),
        ("far_outlier", {
            let mut ts = minutes(300);
            ts.push(i64::MAX - 1);
            ts
        }),
        ("two_points", vec![i64::MIN, i64::MAX]),
    ]
}

/// Values with a flat run, spikes, a level shift and a gap, so most checks do real work.
fn frame(id: &str, ts: Vec<i64>, offset: f64) -> SeriesFrame {
    let n = ts.len();
    let values = (0..n)
        .map(|i| match i {
            _ if i % 97 == 50 => f64::NAN,
            _ if i % 61 == 30 => 1_000.0,
            40..=80 => 5.0,
            _ if i > n / 2 => offset + 50.0 + (i % 7) as f64,
            _ => offset + (i % 7) as f64,
        })
        .collect();
    let quality = (0..n).map(|i| if i % 53 == 7 { Quality::Bad } else { Quality::Good }).collect();
    // Ingest times in reverse order, so latencies span both limits too.
    let ingest: Vec<i64> = ts.iter().rev().copied().collect();
    SeriesFrame::new(SeriesMeta::new(id), ts, values, quality).unwrap().with_ingest_ts(ingest).unwrap()
}

fn assert_windows(label: &str, findings: &[tabayyun_core::Finding]) {
    for f in findings {
        assert!(f.window.start <= f.window.end, "{label}: {} window {:?}", f.check_id, f.window);
    }
}

#[test]
fn single_series_checks_survive_extreme_timestamps() {
    for (name, ts) in layouts() {
        let f = frame("x", ts, 0.0);
        let profile = Profile::compute(&f);
        let ctx = CheckContext::from_frame(&f);
        assert!(ctx.window.start <= ctx.window.end, "{name}: context window");
        for c in [ctx.clone(), ctx.clone().with_profile(profile.clone())] {
            // Defaults, then every bucket and segment width at 1 ns, so their counts reach the
            // whole `i64` span.
            let narrowest = serde_json::json!({"bucket_ns": 1, "segment_ns": 1, "horizon_ns": 1});
            for (id, params) in Registry::builtin_ids()
                .iter()
                .flat_map(|id| [(id, serde_json::Value::Null), (id, narrowest.clone())])
            {
                let cfg = [CheckConfig { id: id.to_string(), params, enabled: true }];
                let out = Registry::run(&cfg, &f, &c).unwrap_or_else(|e| panic!("{name}: {id}: {e}"));
                assert_windows(&format!("{name}: {id}"), &out.findings);
            }
            let all = Registry::run(&Registry::default_configs(), &f, &c).unwrap();
            let report = Scorer::default().score_window("x", &all.findings, c.window);
            assert!((0.0..=100.0).contains(&report.overall), "{name}: score {}", report.overall);
            Scorer::default().score("x", &all.findings);
        }
        let (ts, values) = tabayyun_core::downsample::m4(&f, 4);
        assert_eq!(ts.len(), values.len(), "{name}: m4");
        assert!(ts.windows(2).all(|w| w[0] <= w[1]), "{name}: m4 order");
        assert_eq!((ts.first(), ts.last()), (f.ts.first(), f.ts.last()), "{name}: m4 keeps both ends");
    }
}

/// One group of each kind over members `a` and `b`, with `params` on every group.
fn groups(params: &serde_json::Value, tag: &str) -> Vec<SeriesGroup> {
    let member = |id: &str, role| GroupMember { series_id: id.into(), role };
    let pair = |input, output| vec![member("a", input), member("b", output)];
    [
        (GroupKind::Related, pair(MemberRole::Member, MemberRole::Member)),
        (GroupKind::Redundant, pair(MemberRole::Member, MemberRole::Member)),
        (GroupKind::Balance, pair(MemberRole::Input, MemberRole::Output)),
    ]
    .into_iter()
    .map(|(kind, members)| SeriesGroup {
        id: format!("{kind:?}-{tag}"),
        name: format!("{kind:?} {tag}"),
        kind,
        members,
        params: params.clone(),
    })
    .collect()
}

#[test]
fn cross_checks_survive_extreme_timestamps() {
    // Defaults, then a 1 ns grid and segment, which would need more bins than `align` builds.
    let mut all = groups(&serde_json::Value::Null, "default");
    all.extend(groups(&serde_json::json!({"grid": "1ns", "segment": "1ns"}), "narrowest"));
    for (name, ts) in layouts() {
        let frames = [frame("a", ts.clone(), 0.0), frame("b", ts, 0.5)];
        let profiles: BTreeMap<String, Profile> =
            frames.iter().map(|f| (f.meta.id.clone(), Profile::compute(f))).collect();
        let ctx = CheckContext { now_ns: i64::MAX, window: Window::new(i64::MIN, i64::MAX), profile: None };
        let out = Registry::run_multi(&Registry::default_multi_configs(), &frames, &profiles, &all, &ctx)
            .unwrap_or_else(|e| panic!("{name}: {e}"));
        for (id, o) in &out.per_series {
            assert_windows(&format!("{name}: {id}"), &o.findings);
        }
    }
}
