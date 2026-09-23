//! `tabayyun check-multi` on a synthetic wide CSV (spec 008).

use std::path::PathBuf;
use std::process::Command;

const HOUR: i64 = 3600;

fn scratch(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("tabayyun-cli-{}-{name}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// Three columns over two days of hourly data; `c` is frozen for the second day.
fn wide_csv(dir: &std::path::Path) -> PathBuf {
    let t0 = 1_700_000_000 / HOUR * HOUR;
    let mut rows = vec!["ts,a,b,c".to_string()];
    for i in 0..48 {
        let v = 20.0 + ((i % 24) as f64 / 24.0 * std::f64::consts::TAU).sin() * 5.0 + (i % 7) as f64 * 0.1;
        let c = if i >= 24 { 21.0 } else { v + 1.0 };
        rows.push(format!("{},{v:.3},{:.3},{c:.3}", t0 + i * HOUR, v + 0.5));
    }
    let path = dir.join("wide.csv");
    std::fs::write(&path, rows.join("\n") + "\n").unwrap();
    path
}

#[test]
fn check_multi_prints_per_series_reports_and_skipped_groups() {
    let dir = scratch("reports");
    let csv = wide_csv(&dir);
    let groups = dir.join("groups.json");
    std::fs::write(
        &groups,
        r#"[{"id": "g-ab", "name": "A/B", "kind": "redundant", "members": [{"series_id": "a"}, {"series_id": "b"}]},
            {"id": "g-cx", "name": "C/X", "kind": "related", "members": [{"series_id": "c"}, {"series_id": "x"}]}]"#,
    )
    .unwrap();
    let out = Command::new(env!("CARGO_BIN_EXE_tabayyun"))
        .args([
            "check-multi",
            csv.to_str().unwrap(),
            "--value-cols",
            "a,b,c",
            "--groups",
            groups.to_str().unwrap(),
        ])
        .output()
        .unwrap();
    assert!(out.status.success(), "stderr: {}", String::from_utf8_lossy(&out.stderr));
    let json: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    let reports = json["reports"].as_object().unwrap();
    assert_eq!(reports.keys().collect::<Vec<_>>(), vec!["a", "b", "c"]);
    assert_eq!(reports["a"]["n_samples"], 48);
    let checks = |id: &str| -> Vec<String> {
        reports[id]["findings"]
            .as_array()
            .unwrap()
            .iter()
            .map(|f| f["check_id"].as_str().unwrap().to_string())
            .collect()
    };
    assert!(checks("c").contains(&"tby.flatline".to_string()), "c: {:?}", checks("c"));
    assert!(!checks("a").contains(&"tby.flatline".to_string()));
    assert_eq!(
        json["groups_skipped"],
        serde_json::json!([{"group_id": "g-cx", "reason": "members without data", "missing": ["x"]}])
    );
    std::fs::remove_dir_all(dir).ok();
}

#[test]
fn check_multi_rejects_invalid_groups_and_unknown_columns() {
    let dir = scratch("errors");
    let csv = wide_csv(&dir);
    let groups = dir.join("groups.json");
    std::fs::write(&groups, r#"[{"id": "g", "name": "G", "kind": "balance", "members": [{"series_id": "a", "role": "input"}, {"series_id": "b", "role": "input"}]}]"#).unwrap();
    let run = |cols: &str| {
        Command::new(env!("CARGO_BIN_EXE_tabayyun"))
            .args([
                "check-multi",
                csv.to_str().unwrap(),
                "--value-cols",
                cols,
                "--groups",
                groups.to_str().unwrap(),
            ])
            .output()
            .unwrap()
    };
    let out = run("a,b");
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("need at least one input and one output"));
    let out = run("a,nope");
    assert!(String::from_utf8_lossy(&out.stderr).contains("column `nope` not found"));
    std::fs::remove_dir_all(dir).ok();
}
