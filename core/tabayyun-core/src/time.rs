//! Small duration helpers. All timestamps in the core are `i64` nanoseconds since the Unix
//! epoch (UTC). Durations are `i64` nanoseconds too, which keeps arithmetic branch-free.

pub const NS_PER_SEC: i64 = 1_000_000_000;
pub const NS_PER_MIN: i64 = 60 * NS_PER_SEC;
pub const NS_PER_HOUR: i64 = 60 * NS_PER_MIN;
pub const NS_PER_DAY: i64 = 24 * NS_PER_HOUR;

/// "Nice" sampling intervals that a mode of inter-arrival times is snapped to when it is
/// within 5 %. Anything else is kept verbatim.
pub const NICE_INTERVALS_NS: &[i64] = &[
    NS_PER_SEC / 10,
    NS_PER_SEC / 2,
    NS_PER_SEC,
    2 * NS_PER_SEC,
    5 * NS_PER_SEC,
    10 * NS_PER_SEC,
    15 * NS_PER_SEC,
    30 * NS_PER_SEC,
    NS_PER_MIN,
    2 * NS_PER_MIN,
    5 * NS_PER_MIN,
    10 * NS_PER_MIN,
    15 * NS_PER_MIN,
    30 * NS_PER_MIN,
    NS_PER_HOUR,
    2 * NS_PER_HOUR,
    6 * NS_PER_HOUR,
    12 * NS_PER_HOUR,
    NS_PER_DAY,
];

/// Snap `interval_ns` to the closest nice interval if within 5 %, else return it unchanged.
pub fn snap_to_nice(interval_ns: i64) -> i64 {
    if interval_ns <= 0 {
        return interval_ns;
    }
    let mut best = interval_ns;
    let mut best_rel = f64::INFINITY;
    for &nice in NICE_INTERVALS_NS {
        let rel = ((nice - interval_ns).abs() as f64) / (interval_ns as f64);
        if rel < best_rel {
            best_rel = rel;
            best = nice;
        }
    }
    if best_rel <= 0.05 {
        best
    } else {
        interval_ns
    }
}

/// Parse a human duration such as `5m`, `90s`, `2h`, `1d`, `250ms` into nanoseconds.
pub fn parse_duration(s: &str) -> Option<i64> {
    let s = s.trim();
    let split = s.find(|c: char| !c.is_ascii_digit() && c != '.')?;
    let (num, unit) = s.split_at(split);
    let n: f64 = num.parse().ok()?;
    let mult = match unit.trim() {
        "ns" => 1.0,
        "us" | "µs" => 1e3,
        "ms" => 1e6,
        "s" => NS_PER_SEC as f64,
        "m" | "min" => NS_PER_MIN as f64,
        "h" => NS_PER_HOUR as f64,
        "d" => NS_PER_DAY as f64,
        _ => return None,
    };
    Some((n * mult).round() as i64)
}

/// Render nanoseconds as a compact human duration, e.g. `1h30m`, `45s`, `250ms`.
pub fn format_duration(ns: i64) -> String {
    let neg = ns < 0;
    let mut rem = ns.abs();
    let mut parts = Vec::new();
    for (unit, size) in [("d", NS_PER_DAY), ("h", NS_PER_HOUR), ("m", NS_PER_MIN), ("s", NS_PER_SEC)] {
        if rem >= size {
            parts.push(format!("{}{}", rem / size, unit));
            rem %= size;
        }
        if parts.len() == 2 {
            break;
        }
    }
    if parts.is_empty() {
        if rem >= 1_000_000 {
            parts.push(format!("{}ms", rem / 1_000_000));
        } else {
            parts.push(format!("{}ns", rem));
        }
    }
    let s = parts.join("");
    if neg {
        format!("-{s}")
    } else {
        s
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_and_formats() {
        assert_eq!(parse_duration("5m"), Some(5 * NS_PER_MIN));
        assert_eq!(parse_duration("1.5h"), Some(90 * NS_PER_MIN));
        assert_eq!(parse_duration("250ms"), Some(250_000_000));
        assert_eq!(parse_duration("x"), None);
        assert_eq!(format_duration(90 * NS_PER_MIN), "1h30m");
        assert_eq!(format_duration(45 * NS_PER_SEC), "45s");
    }

    #[test]
    fn snaps_close_intervals_only() {
        assert_eq!(snap_to_nice(59_900_000_000), NS_PER_MIN);
        assert_eq!(snap_to_nice(7 * NS_PER_SEC), 7 * NS_PER_SEC);
    }
}
