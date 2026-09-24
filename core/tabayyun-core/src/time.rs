//! Small duration helpers. All timestamps in the core are `i64` nanoseconds since the Unix
//! epoch (UTC). Durations are `i64` nanoseconds too, which keeps arithmetic branch-free.

pub const NS_PER_SEC: i64 = 1_000_000_000;
pub const NS_PER_MIN: i64 = 60 * NS_PER_SEC;
pub const NS_PER_HOUR: i64 = 60 * NS_PER_MIN;
pub const NS_PER_DAY: i64 = 24 * NS_PER_HOUR;

/// Unit of epoch-integer timestamps (ADR-0014, spec 017).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TsUnit {
    S,
    Ms,
    Us,
    Ns,
}

impl TsUnit {
    /// Short name as used in files and APIs: `s`, `ms`, `us` or `ns`.
    pub fn as_str(self) -> &'static str {
        match self {
            TsUnit::S => "s",
            TsUnit::Ms => "ms",
            TsUnit::Us => "us",
            TsUnit::Ns => "ns",
        }
    }

    /// Long name for messages.
    pub fn name(self) -> &'static str {
        match self {
            TsUnit::S => "seconds",
            TsUnit::Ms => "milliseconds",
            TsUnit::Us => "microseconds",
            TsUnit::Ns => "nanoseconds",
        }
    }

    /// Nanoseconds per unit.
    pub fn ns(self) -> i64 {
        match self {
            TsUnit::S => NS_PER_SEC,
            TsUnit::Ms => 1_000_000,
            TsUnit::Us => 1_000,
            TsUnit::Ns => 1,
        }
    }

    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "s" => Some(TsUnit::S),
            "ms" => Some(TsUnit::Ms),
            "us" => Some(TsUnit::Us),
            "ns" => Some(TsUnit::Ns),
            _ => None,
        }
    }
}

/// Epoch-integer instants must fall in `[1971-01-01, 2200-01-01)` UTC (ADR-0014): a date in
/// 1970 or centuries ahead means the unit is wrong. Older data is read as text.
pub const PLAUSIBLE_RANGE_NS: (i64, i64) = (31_536_000 * NS_PER_SEC, 7_258_118_400 * NS_PER_SEC);

/// Unit of an epoch-integer column from the median of its absolute values, with ADR-0014's
/// thresholds as half-open ranges: below 1e11 seconds, below 1e14 milliseconds, below 1e17
/// microseconds, else nanoseconds. The API's `infer_epoch_unit` uses the same table
/// (`tests/data/epoch_cases.json`).
pub fn infer_epoch_unit(median_abs: i64) -> TsUnit {
    match median_abs {
        x if x < 100_000_000_000 => TsUnit::S,
        x if x < 100_000_000_000_000 => TsUnit::Ms,
        x if x < 100_000_000_000_000_000 => TsUnit::Us,
        _ => TsUnit::Ns,
    }
}

/// `value` read in `unit` as ns since the epoch; `None` when it overflows or falls outside
/// [`PLAUSIBLE_RANGE_NS`].
pub fn epoch_to_ns(value: i64, unit: TsUnit) -> Option<i64> {
    value.checked_mul(unit.ns()).filter(|ns| (PLAUSIBLE_RANGE_NS.0..PLAUSIBLE_RANGE_NS.1).contains(ns))
}

/// An epoch-integer value that does not convert to a plausible instant.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EpochError {
    /// Zero-based position in the column.
    pub index: usize,
    pub value: i64,
    pub unit: TsUnit,
}

impl std::fmt::Display for EpochError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} read as {} is not between 1971-01-01 and 2199-12-31", self.value, self.unit.name())
    }
}

impl std::error::Error for EpochError {}

/// Convert a whole epoch-integer column with one unit: the declared one, else the unit of
/// its median absolute value. Every value must convert (see [`epoch_to_ns`]); the first
/// that does not is the error. An empty column reads as nanoseconds, as the API's does.
pub fn resolve_epoch_column(values: &[i64], unit: Option<TsUnit>) -> Result<(Vec<i64>, TsUnit), EpochError> {
    let unit = unit.unwrap_or_else(|| {
        if values.is_empty() {
            return TsUnit::Ns;
        }
        let mut abs: Vec<u64> = values.iter().map(|v| v.unsigned_abs()).collect();
        let mid = (abs.len() - 1) / 2;
        let (_, median, _) = abs.select_nth_unstable(mid);
        infer_epoch_unit(i64::try_from(*median).unwrap_or(i64::MAX))
    });
    let ns = values
        .iter()
        .enumerate()
        .map(|(index, &value)| epoch_to_ns(value, unit).ok_or(EpochError { index, value, unit }))
        .collect::<Result<Vec<i64>, EpochError>>()?;
    Ok((ns, unit))
}

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

    /// The table shared with `api/tests/test_ts_unit.py`.
    fn cases() -> serde_json::Value {
        serde_json::from_str(include_str!("../tests/data/epoch_cases.json")).unwrap()
    }

    #[test]
    fn infer_thresholds() {
        for c in cases()["infer"].as_array().unwrap() {
            let m = c["median_abs"].as_i64().unwrap();
            assert_eq!(infer_epoch_unit(m).as_str(), c["unit"], "median {m}");
        }
    }

    #[test]
    fn range_check() {
        for c in cases()["convert"].as_array().unwrap() {
            let (v, unit) =
                (c["value"].as_i64().unwrap(), TsUnit::parse(c["unit"].as_str().unwrap()).unwrap());
            assert_eq!(epoch_to_ns(v, unit), c["ns"].as_i64(), "{v} {unit:?}");
        }
    }

    #[test]
    fn column_takes_one_unit_from_its_median() {
        // Mostly seconds with one millisecond value: the column is seconds, and the stray
        // value is the error, not silently read as milliseconds.
        let col = [1_700_000_000, 1_700_000_060, 1_700_000_120_000, 1_700_000_180];
        let err = resolve_epoch_column(&col, None).unwrap_err();
        assert_eq!(err, EpochError { index: 2, value: 1_700_000_120_000, unit: TsUnit::S });
        assert!(err.to_string().contains("read as seconds"), "{err}");
        let (ns, unit) = resolve_epoch_column(&col[..2], None).unwrap();
        assert_eq!((ns, unit), (vec![1_700_000_000_000_000_000, 1_700_000_060_000_000_000], TsUnit::S));
        // A declared unit wins over the median.
        assert_eq!(resolve_epoch_column(&[1_700_000_000_000], Some(TsUnit::Ms)).unwrap().1, TsUnit::Ms);
        assert_eq!(resolve_epoch_column(&[], None).unwrap(), (vec![], TsUnit::Ns));
        // i64::MIN has no positive twin; the median still works.
        assert!(resolve_epoch_column(&[i64::MIN], None).is_err());
    }

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
