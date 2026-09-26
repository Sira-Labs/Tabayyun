//! `tby.correlation_break` — related series stopped agreeing (catalogue #22, spec 009).
//!
//! For every pair of a `related` or `redundant` group the pair is aligned (spec 008) and cut
//! into UTC segments. Per segment the check computes Spearman's ρ over complete bins and the
//! lag (in grid steps) that maximises the cross-correlation of first differences, signed by
//! the pair's direction. The first `ref_segments` usable
//! segments form the reference and only later segments are judged, so a segment never takes
//! part in its own reference. Consecutive broken segments form one episode and one finding
//! (ADR-0011); lag episodes are separate findings with their own evidence shape.
//!
//! A finding attaches to the pair member that comes first in the group, with the other in
//! `partner`; metrics are named `rho:<partner>` and `lag_steps:<partner>` so the pairs of one
//! series do not overwrite each other's points.

use super::{duration_param, episodes, CheckContext, CheckOutput};
use crate::align::align;
use crate::cross::{with_group_params, CrossCheck, GroupKind, SeriesGroup};
use crate::error::{Error, Result};
use crate::finding::{Dimension, Finding, Metric, Severity, Window};
use crate::frame::SeriesFrame;
use crate::profile::median_mad;
use crate::time::format_duration;
use serde::{Deserialize, Serialize};

pub const ID: &str = "tby.correlation_break";
/// Below this many reference segments the reference itself is not trustworthy.
const MIN_REFERENCE_SEGMENTS: usize = 4;
/// A sign flip only counts when the new correlation is clearly away from zero.
const SIGN_FLIP_MIN: f64 = 0.2;
/// A lag moves when it differs from the reference by more than this many steps.
const LAG_TOLERANCE: i64 = 1;
/// Upper bound on `max_lag`: each segment scans 2 × max_lag + 1 lags.
const MAX_LAG: usize = 240;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct CorrelationBreak {
    /// Segment length ρ is computed over, e.g. `1d`.
    pub segment: String,
    /// Flag when |ρ − ρ_ref| exceeds this.
    pub delta: f64,
    /// Below this |ρ_ref| the pair is not related enough to judge (metrics only).
    pub min_ref: f64,
    /// Complete aligned bins a segment needs.
    pub min_points: usize,
    /// Leading usable segments that form the reference.
    pub ref_segments: usize,
    /// Lags searched, in grid steps, on either side.
    pub max_lag: usize,
    /// A moved lag must correlate at least this much better than the reference lag in the
    /// same segment; on smooth, noisy pairs neighbouring lags fit almost equally well.
    pub lag_margin: f64,
    /// Alignment grid: `auto` (coarsest expected interval) or a duration.
    pub grid: String,
    pub severity: Severity,
}

impl Default for CorrelationBreak {
    fn default() -> Self {
        Self {
            segment: "1d".into(),
            delta: 0.3,
            min_ref: 0.5,
            min_points: 24,
            ref_segments: 7,
            max_lag: 6,
            lag_margin: 0.1,
            grid: "auto".into(),
            severity: Severity::High,
        }
    }
}

/// Ranks starting at 1, ties sharing their average rank.
pub(crate) fn average_ranks(xs: &[f64]) -> Vec<f64> {
    let mut idx: Vec<usize> = (0..xs.len()).collect();
    idx.sort_by(|&a, &b| xs[a].total_cmp(&xs[b]));
    let mut ranks = vec![0.0; xs.len()];
    let mut i = 0;
    while i < idx.len() {
        let mut j = i;
        while j + 1 < idx.len() && xs[idx[j + 1]] == xs[idx[i]] {
            j += 1;
        }
        let rank = (i + j) as f64 / 2.0 + 1.0;
        for &k in &idx[i..=j] {
            ranks[k] = rank;
        }
        i = j + 1;
    }
    ranks
}

/// Pearson correlation; None with fewer than 3 pairs or a constant side.
pub(crate) fn pearson(x: &[f64], y: &[f64]) -> Option<f64> {
    let n = x.len().min(y.len());
    if n < 3 {
        return None;
    }
    let (mx, my) = (x[..n].iter().sum::<f64>() / n as f64, y[..n].iter().sum::<f64>() / n as f64);
    let (mut sxy, mut sxx, mut syy) = (0.0, 0.0, 0.0);
    for i in 0..n {
        let (dx, dy) = (x[i] - mx, y[i] - my);
        sxy += dx * dy;
        sxx += dx * dx;
        syy += dy * dy;
    }
    (sxx > 0.0 && syy > 0.0).then(|| (sxy / (sxx * syy).sqrt()).clamp(-1.0, 1.0))
}

/// Spearman's ρ: Pearson on average ranks.
pub(crate) fn spearman(x: &[f64], y: &[f64]) -> Option<f64> {
    pearson(&average_ranks(x), &average_ranks(y))
}

/// First differences; a step touching a NaN bin is NaN.
fn diff(xs: &[f64]) -> Vec<f64> {
    xs.windows(2).map(|w| w[1] - w[0]).collect()
}

/// `sign` × corr(x[t], y[t + lag]) for every lag in `[-max_lag, max_lag]` (index `lag +
/// max_lag`) over pairwise-complete bins; None where fewer than `min_pairs` pairs remain.
/// `sign` is the pair's direction, so a negatively related pair peaks where it is most
/// negatively correlated.
fn lag_profile(x: &[f64], y: &[f64], sign: f64, max_lag: usize, min_pairs: usize) -> Vec<Option<f64>> {
    let n = x.len() as i64;
    (-(max_lag as i64)..=(max_lag as i64))
        .map(|lag| {
            let (mut xs, mut ys) = (Vec::new(), Vec::new());
            for t in 0.max(-lag)..n.min(n - lag) {
                let (a, b) = (x[t as usize], y[(t + lag) as usize]);
                if a.is_finite() && b.is_finite() {
                    xs.push(a);
                    ys.push(b);
                }
            }
            if xs.len() < min_pairs.max(3) {
                return None;
            }
            pearson(&xs, &ys).map(|r| sign * r)
        })
        .collect()
}

/// The profile's peak as (lag, strength); ties go to the smaller |lag|, so a flat profile
/// reads as no lag.
fn best_lag(profile: &[Option<f64>], max_lag: usize) -> Option<(i64, f64)> {
    let mut best: Option<(i64, f64)> = None;
    for (k, r) in profile.iter().enumerate() {
        let (Some(r), lag) = (*r, k as i64 - max_lag as i64) else { continue };
        let better = match best {
            None => true,
            Some((l, c)) => r > c + 1e-12 || ((r - c).abs() <= 1e-12 && lag.abs() < l.abs()),
        };
        if better {
            best = Some((lag, r));
        }
    }
    best
}

/// One usable segment of an aligned pair.
struct Segment {
    /// Calendar segment index (`start / segment_ns`), so a skipped day separates episodes.
    key: usize,
    window: Window,
    n_points: usize,
    rho: f64,
    lag: Option<(i64, f64)>,
    /// Signed cross-correlation per lag (see `lag_profile`), to test a moved lag against the
    /// reference lag within the same segment.
    profile: Vec<Option<f64>>,
}

fn median(xs: &[f64]) -> f64 {
    median_mad(xs).map_or(f64::NAN, |(m, _)| m)
}

/// How the partner moves relative to the first series at `lag` grid steps.
fn timing(lag: i64, grid_ns: i64) -> String {
    match lag.signum() {
        1 => format!("lags by {}", format_duration(lag * grid_ns)),
        -1 => format!("leads by {}", format_duration(-lag * grid_ns)),
        _ => "moves in step".into(),
    }
}

fn name(f: &SeriesFrame) -> &str {
    f.meta.name.as_deref().unwrap_or(&f.meta.id)
}

impl CorrelationBreak {
    fn segments(
        &self,
        a: &SeriesFrame,
        b: &SeriesFrame,
        segment_ns: i64,
        grid: Option<i64>,
    ) -> (i64, Vec<Segment>) {
        let aligned = align(&[a, b], grid);
        let (x, y) = (&aligned.columns[0], &aligned.columns[1]);
        let mut out = Vec::new();
        let mut s = 0;
        while s < aligned.len() {
            let key = aligned.ts[s].div_euclid(segment_ns);
            let mut e = s;
            while e < aligned.len() && aligned.ts[e].div_euclid(segment_ns) == key {
                e += 1;
            }
            let complete: Vec<usize> = (s..e).filter(|&i| aligned.complete(i)).collect();
            if complete.len() >= self.min_points.max(3) {
                let xs: Vec<f64> = complete.iter().map(|&i| x[i]).collect();
                let ys: Vec<f64> = complete.iter().map(|&i| y[i]).collect();
                if let Some(rho) = spearman(&xs, &ys) {
                    // On first differences: levels of slowly trending series correlate at
                    // every lag, which flattens the profile the lag is read from.
                    let (dx, dy) = (diff(&x[s..e]), diff(&y[s..e]));
                    let profile = lag_profile(&dx, &dy, rho.signum(), self.max_lag, self.min_points / 2);
                    let lag = best_lag(&profile, self.max_lag);
                    let window = Window::new(
                        aligned.ts[complete[0]],
                        aligned.ts[complete[complete.len() - 1]] + aligned.grid_ns,
                    );
                    out.push(Segment {
                        key: key as usize,
                        window,
                        n_points: complete.len(),
                        rho,
                        lag,
                        profile,
                    });
                }
            }
            s = e;
        }
        (aligned.grid_ns, out)
    }
}

impl CrossCheck for CorrelationBreak {
    fn id(&self) -> &'static str {
        ID
    }
    fn dimension(&self) -> Dimension {
        Dimension::Consistency
    }
    fn default_severity(&self) -> Severity {
        self.severity
    }
    fn kinds(&self) -> &'static [GroupKind] {
        &[GroupKind::Related, GroupKind::Redundant]
    }

    fn run(&self, frames: &[&SeriesFrame], group: &SeriesGroup, ctx: &CheckContext) -> Result<CheckOutput> {
        let p = with_group_params(self, ID, group)?;
        let segment_ns = duration_param(ID, "segment", &p.segment)?;
        let grid = if p.grid == "auto" { None } else { Some(duration_param(ID, "grid", &p.grid)?) };
        let invalid = |reason: &str| Error::InvalidParams {
            check: ID.into(),
            reason: format!("group {}: {reason}", group.id),
        };
        // `parse_duration` accepts "0s": a zero segment would divide by zero, and a zero grid
        // would silently fall back to the inferred one.
        if segment_ns <= 0 {
            return Err(invalid("segment must be positive"));
        }
        if grid.is_some_and(|g| g <= 0) {
            return Err(invalid("grid must be positive"));
        }
        if p.max_lag > MAX_LAG {
            return Err(invalid(&format!("max_lag must be at most {MAX_LAG} steps")));
        }
        if !(0.0..=2.0).contains(&p.lag_margin) {
            return Err(invalid("lag_margin must be between 0 and 2"));
        }
        let mut out = CheckOutput::default();
        for i in 0..frames.len() {
            for j in (i + 1)..frames.len() {
                p.pair(frames[i], frames[j], group, segment_ns, grid, ctx, &mut out);
            }
        }
        Ok(out)
    }
}

impl CorrelationBreak {
    #[allow(clippy::too_many_arguments)]
    fn pair(
        &self,
        a: &SeriesFrame,
        b: &SeriesFrame,
        group: &SeriesGroup,
        segment_ns: i64,
        grid: Option<i64>,
        ctx: &CheckContext,
        out: &mut CheckOutput,
    ) {
        let (grid_ns, segs) = self.segments(a, b, segment_ns, grid);
        let partner = b.meta.id.as_str();
        let metric = |name: &str, ts: i64, value: f64| Metric {
            check_id: ID.into(),
            series_id: a.meta.id.clone(),
            name: format!("{name}:{partner}"),
            ts,
            value,
        };
        for s in &segs {
            out.metrics.push(metric("rho", s.window.start, s.rho));
            if let Some((lag, _)) = s.lag {
                out.metrics.push(metric("lag_steps", s.window.start, lag as f64));
            }
        }
        let n_ref = self.ref_segments;
        if n_ref < MIN_REFERENCE_SEGMENTS || segs.len() <= n_ref {
            let reason = format!(
                "insufficient baseline ({} usable segments, pair {}/{partner})",
                segs.len(),
                a.meta.id
            );
            out.skipped.push((ID.into(), reason));
            return;
        }
        let (reference, judged) = segs.split_at(n_ref);
        let rho_ref = median(&reference.iter().map(|s| s.rho).collect::<Vec<_>>());
        if rho_ref.abs() < self.min_ref {
            return; // not a related pair in this window: metrics only
        }
        let broken = |s: &Segment| {
            (s.rho - rho_ref).abs() > self.delta
                || (s.rho.signum() != rho_ref.signum() && s.rho.abs() > SIGN_FLIP_MIN)
        };
        let base = |extra: serde_json::Value| {
            let mut ev = group.evidence_base();
            ev.insert("partner".into(), partner.into());
            if let serde_json::Value::Object(m) = extra {
                ev.extend(m);
            }
            serde_json::Value::Object(ev)
        };
        let fraction = |w: &Window| w.duration() as f64 / ctx.window.duration().max(1) as f64;

        let flagged: Vec<(usize, Window, &Segment)> =
            judged.iter().filter(|s| broken(s)).map(|s| (s.key, s.window, s)).collect();
        for (w, items) in episodes(flagged) {
            let worst = items
                .iter()
                .max_by(|x, y| (x.rho - rho_ref).abs().total_cmp(&(y.rho - rho_ref).abs()))
                .unwrap();
            let verb = if worst.rho.signum() != rho_ref.signum() && worst.rho.abs() > SIGN_FLIP_MIN {
                "moved against"
            } else {
                "stopped tracking"
            };
            let summary = format!(
                "{} {verb} {} for {} (ρ {:.2}, usually {:.2})",
                name(a),
                name(b),
                format_duration(w.duration()),
                worst.rho,
                rho_ref
            );
            let evidence = base(serde_json::json!({
                "rho": worst.rho, "rho_ref": rho_ref, "delta": self.delta,
                "n_segments": items.len(), "n_points": items.iter().map(|s| s.n_points).sum::<usize>(),
            }));
            out.findings.push(Finding::new(
                ID,
                &a.meta.id,
                Dimension::Consistency,
                self.severity,
                w,
                fraction(&w),
                summary,
                evidence,
            ));
        }

        // Lag: judged only where the correlation held and the best cross-correlation is
        // itself strong, so a decoupled segment's random lag is not reported as a second finding.
        let ref_lags: Vec<f64> = reference.iter().filter_map(|s| s.lag).map(|(l, _)| l as f64).collect();
        // A reference lag that wanders by more than the tolerance is no reference: the pair's
        // cross-correlation has no sharp peak, and any "moved" lag would be noise.
        let Some((lag_median, lag_mad)) =
            median_mad(&ref_lags).filter(|_| ref_lags.len() >= MIN_REFERENCE_SEGMENTS)
        else {
            return;
        };
        if lag_mad > LAG_TOLERANCE as f64 {
            return;
        }
        let lag_ref = lag_median.round() as i64;
        let lag_flagged: Vec<(usize, Window, (&Segment, i64))> = judged
            .iter()
            .filter(|s| !broken(s))
            .filter_map(|s| s.lag.filter(|(_, r)| *r >= self.min_ref).map(|(l, _)| (s, l)))
            .filter(|(_, l)| (l - lag_ref).abs() > LAG_TOLERANCE)
            .filter(|(s, l)| {
                let at = |lag: i64| {
                    s.profile.get(usize::try_from(lag + self.max_lag as i64).ok()?).copied().flatten()
                };
                // The reference lag may lie outside this segment's usable lags: then nothing to
                // compare with, and the moved lag stands.
                at(lag_ref).is_none_or(|r| at(*l).is_some_and(|b| b - r >= self.lag_margin))
            })
            .map(|(s, l)| (s.key, s.window, (s, l)))
            .collect();
        for (w, items) in episodes(lag_flagged) {
            let &(_, lag) = items.iter().max_by_key(|(_, l)| (l - lag_ref).abs()).unwrap();
            let summary = format!(
                "{} {} relative to {} for {} (usually {})",
                name(b),
                timing(lag, grid_ns),
                name(a),
                format_duration(w.duration()),
                timing(lag_ref, grid_ns)
            );
            let evidence = base(serde_json::json!({
                "lag_steps": lag, "lag_ref_steps": lag_ref, "grid_ns": grid_ns, "n_segments": items.len(),
            }));
            out.findings.push(Finding::new(
                ID,
                &a.meta.id,
                Dimension::Consistency,
                self.severity,
                w,
                fraction(&w),
                summary,
                evidence,
            ));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cross::{GroupMember, MemberRole};
    use crate::frame::SeriesMeta;
    use crate::synth::Rng;
    use crate::time::{NS_PER_DAY, NS_PER_MIN};

    const STEP: i64 = 15 * NS_PER_MIN;
    const PER_DAY: usize = 96;
    const DAYS: usize = 14;
    const T0: i64 = 1_704_067_200 * 1_000_000_000; // 2024-01-01, a day boundary

    /// A smooth common driver: AR(1) with φ = 0.95 plus a daily sine.
    fn driver(n: usize, seed: u64) -> Vec<f64> {
        let mut rng = Rng::new(seed);
        let mut ar = 0.0;
        (0..n)
            .map(|i| {
                ar = 0.95 * ar + rng.normal();
                ar + 2.0 * (i as f64 / PER_DAY as f64 * std::f64::consts::TAU).sin()
            })
            .collect()
    }

    fn frame(id: &str, values: Vec<f64>) -> SeriesFrame {
        let ts = (0..values.len() as i64).map(|i| T0 + i * STEP).collect();
        let mut meta = SeriesMeta::new(id);
        meta.name = Some(id.to_uppercase());
        SeriesFrame::with_default_quality(meta, ts, values).unwrap()
    }

    /// `x` follows the driver; `y` follows it too except on `days`, where `alter` rewrites it.
    fn pair(
        days: std::ops::Range<usize>,
        alter: impl Fn(usize, &[f64], &mut Rng) -> f64,
    ) -> (SeriesFrame, SeriesFrame) {
        let n = DAYS * PER_DAY;
        let d = driver(n, 7);
        let mut rng = Rng::new(11);
        let x: Vec<f64> = d.iter().map(|v| v + 0.05 * rng.normal()).collect();
        let y: Vec<f64> = (0..n)
            .map(|i| {
                if days.contains(&(i / PER_DAY)) {
                    alter(i, &d, &mut rng)
                } else {
                    d[i] + 0.05 * rng.normal()
                }
            })
            .collect();
        (frame("pt-a", x), frame("pt-b", y))
    }

    fn group(kind: GroupKind, ids: &[&str], params: serde_json::Value) -> SeriesGroup {
        SeriesGroup {
            id: "g".into(),
            name: "PT".into(),
            kind,
            members: ids
                .iter()
                .map(|s| GroupMember { series_id: s.to_string(), role: MemberRole::Member })
                .collect(),
            params,
        }
    }

    fn run(frames: &[&SeriesFrame], params: serde_json::Value) -> CheckOutput {
        let ctx = CheckContext::from_frame(frames[0]);
        let ids: Vec<&str> = frames.iter().map(|f| f.meta.id.as_str()).collect();
        CorrelationBreak::default().run(frames, &group(GroupKind::Redundant, &ids, params), &ctx).unwrap()
    }

    fn day(k: usize) -> i64 {
        T0 + k as i64 * NS_PER_DAY
    }

    #[test]
    fn decoupled_day() {
        let (a, b) = pair(10..11, |_, _, rng| 3.0 * rng.normal());
        let out = run(&[&a, &b], serde_json::Value::Null);
        assert_eq!(out.findings.len(), 1, "{:?}", out.findings);
        let f = &out.findings[0];
        assert_eq!((f.series_id.as_str(), f.window.start, f.window.end), ("pt-a", day(10), day(11)));
        assert_eq!(f.evidence["partner"], "pt-b");
        assert_eq!(f.evidence["group_id"], "g");
        assert!(
            f.evidence["rho"].as_f64().unwrap().abs() < 0.3 && f.evidence["rho_ref"].as_f64().unwrap() > 0.9
        );
        assert!(f.summary.starts_with("PT-A stopped tracking PT-B for 1d"), "{}", f.summary);
        // One rho point per day, named after the partner.
        assert_eq!(out.metrics.iter().filter(|m| m.name == "rho:pt-b").count(), DAYS);
        assert!(out.skipped.is_empty());
    }

    #[test]
    fn sign_flip() {
        let (a, b) = pair(9..11, |i, d, rng| -d[i] + 0.05 * rng.normal());
        let out = run(&[&a, &b], serde_json::Value::Null);
        assert_eq!(out.findings.len(), 1, "{:?}", out.findings);
        let f = &out.findings[0];
        assert_eq!((f.window.start, f.window.end), (day(9), day(11)));
        assert!(f.evidence["rho"].as_f64().unwrap() < -0.8);
        assert_eq!(f.evidence["n_segments"], 2);
        assert!(f.summary.contains("moved against"), "{}", f.summary);
    }

    #[test]
    fn lag_shift() {
        let (a, b) = pair(11..12, |i, d, rng| d[i - 3] + 0.05 * rng.normal());
        let out = run(&[&a, &b], serde_json::Value::Null);
        assert_eq!(out.findings.len(), 1, "{:?}", out.findings);
        let f = &out.findings[0];
        assert!(f.evidence.get("rho").is_none(), "expected a lag finding: {}", f.evidence);
        assert_eq!(
            (f.evidence["lag_steps"].as_i64(), f.evidence["lag_ref_steps"].as_i64()),
            (Some(3), Some(0))
        );
        assert_eq!(f.evidence["grid_ns"], STEP);
        assert_eq!((f.window.start, f.window.end), (day(11), day(12)));
        assert_eq!(f.summary, "PT-B lags by 45m relative to PT-A for 1d (usually moves in step)");
    }

    #[test]
    fn smooth_noisy_pair_has_no_lag_findings() {
        // Two meters on one pipe: a slow daily curve at 15-minute bins plus independent
        // noise. Neighbouring lags of the differenced series fit almost equally well, so the
        // peak wanders by a step or two from day to day; none of that is a moved lag.
        let n = 42 * PER_DAY;
        let mut rng = Rng::new(3);
        let curve: Vec<f64> = (0..n)
            .map(|i| {
                let weekend = if (i / PER_DAY) % 7 >= 5 { 0.8 } else { 1.0 };
                let hour = (i % PER_DAY) as f64 / PER_DAY as f64 * 24.0;
                20.0 + 120.0 * weekend * (1.0 + 0.6 * ((hour - 9.0) / 24.0 * std::f64::consts::TAU).sin())
            })
            .collect();
        let meter = |rng: &mut Rng| curve.iter().map(|v| v + 0.5 * rng.normal()).collect::<Vec<_>>();
        let (a, b) = (frame("ft-a", meter(&mut rng)), frame("ft-b", meter(&mut rng)));
        let silent = run(&[&a, &b], serde_json::Value::Null);
        assert!(silent.findings.is_empty(), "{:?}", silent.findings);
        // Without the margin the wandering peak is reported: the case the margin exists for.
        let noisy = run(&[&a, &b], serde_json::json!({"lag_margin": 0.0}));
        assert!(!noisy.findings.is_empty(), "expected spurious lag findings without a margin");
    }

    #[test]
    fn negatively_related_pair_lag() {
        // y mirrors the driver (ρ ≈ −1); on day 11 it mirrors it three steps late.
        let n = DAYS * PER_DAY;
        let d = driver(n, 7);
        let mut rng = Rng::new(5);
        let a = frame("pt-a", d.clone());
        let y =
            (0..n).map(|i| if i / PER_DAY == 11 { -d[i - 3] } else { -d[i] } + 0.05 * rng.normal()).collect();
        let out = run(&[&a, &frame("pt-b", y)], serde_json::Value::Null);
        assert_eq!(out.findings.len(), 1, "{:?}", out.findings);
        let ev = &out.findings[0].evidence;
        assert_eq!((ev["lag_steps"].as_i64(), ev["lag_ref_steps"].as_i64()), (Some(3), Some(0)));
    }

    #[test]
    fn independent_pair_is_silent() {
        let n = DAYS * PER_DAY;
        let a = frame("pt-a", driver(n, 1));
        let b = frame("pt-b", driver(n, 2));
        let out = run(&[&a, &b], serde_json::Value::Null);
        assert!(out.findings.is_empty(), "{:?}", out.findings);
        assert!(out.skipped.is_empty());
        assert_eq!(out.metrics.iter().filter(|m| m.name == "rho:pt-b").count(), DAYS);
    }

    #[test]
    fn too_few_segments_skips() {
        let n = 5 * PER_DAY;
        let d = driver(n, 3);
        let a = frame("pt-a", d.clone());
        let b = frame("pt-b", d.iter().map(|v| v * 2.0).collect());
        let out = run(&[&a, &b], serde_json::Value::Null);
        assert!(out.findings.is_empty());
        assert_eq!(out.skipped.len(), 1);
        assert_eq!(out.skipped[0].0, ID);
        assert!(
            out.skipped[0].1.starts_with("insufficient baseline (5 usable segments"),
            "{}",
            out.skipped[0].1
        );
        // Group params override the defaults: with 4 reference days the pair is judged.
        let out = run(&[&a, &b], serde_json::json!({"ref_segments": 4, "k": 9}));
        assert!(out.skipped.is_empty() && out.findings.is_empty());
    }

    #[test]
    fn ties_rank_correctly() {
        assert_eq!(average_ranks(&[10.0, 20.0, 20.0, 30.0]), vec![1.0, 2.5, 2.5, 4.0]);
        assert_eq!(average_ranks(&[3.0, 1.0, 3.0, 3.0]), vec![3.0, 1.0, 3.0, 3.0]);
        let r = spearman(&[1.0, 2.0, 2.0, 3.0], &[1.0, 2.0, 2.0, 3.0]).unwrap();
        assert!((r - 1.0).abs() < 1e-12);
        assert!((spearman(&[1.0, 2.0, 3.0, 4.0], &[8.0, 4.0, 2.0, 1.0]).unwrap() + 1.0).abs() < 1e-12);
        assert!(spearman(&[1.0, 1.0, 1.0], &[1.0, 2.0, 3.0]).is_none());
    }

    #[test]
    fn every_pair_of_a_triple_is_judged_and_named() {
        let (a, b) = pair(10..11, |_, _, rng| 3.0 * rng.normal());
        let c = frame("pt-c", a.values.iter().map(|v| v + 1.0).collect());
        let out = run(&[&a, &b, &c], serde_json::Value::Null);
        // b decouples from both a and c; a and c keep agreeing.
        let mut pairs: Vec<(String, String)> = out
            .findings
            .iter()
            .map(|f| (f.series_id.clone(), f.evidence["partner"].as_str().unwrap().to_string()))
            .collect();
        pairs.sort();
        assert_eq!(pairs, vec![("pt-a".into(), "pt-b".into()), ("pt-b".into(), "pt-c".into())]);
        assert!(out.metrics.iter().any(|m| m.series_id == "pt-a" && m.name == "rho:pt-c"));
    }

    #[test]
    fn bad_group_params_are_invalid_params() {
        let (a, b) = pair(0..0, |_, _, _| 0.0);
        let ctx = CheckContext::from_frame(&a);
        for (params, message) in [
            (serde_json::json!({"delta": "high"}), "invalid type"),
            (serde_json::json!({"segment": "0s"}), "segment must be positive"),
            (serde_json::json!({"grid": "0s"}), "grid must be positive"),
            (serde_json::json!({"max_lag": 241}), "max_lag must be at most 240"),
            (serde_json::json!({"lag_margin": -0.1}), "lag_margin must be between 0 and 2"),
        ] {
            let g = group(GroupKind::Related, &["pt-a", "pt-b"], params);
            let err = CorrelationBreak::default().run(&[&a, &b], &g, &ctx).unwrap_err();
            assert!(matches!(err, Error::InvalidParams { .. }), "{err}");
            assert!(err.to_string().contains(message), "{err}");
        }
    }

    #[test]
    fn registry_runs_it_on_redundant_groups() {
        use crate::registry::Registry;
        use std::collections::BTreeMap;
        let (a, b) = pair(10..11, |_, _, rng| 3.0 * rng.normal());
        let ctx = CheckContext::from_frame(&a);
        let groups = vec![group(GroupKind::Redundant, &["pt-a", "pt-b"], serde_json::Value::Null)];
        let configs = vec![crate::registry::CheckConfig {
            id: ID.into(),
            params: serde_json::Value::Null,
            enabled: true,
        }];
        let out = Registry::run_multi(&configs, &[a, b], &BTreeMap::new(), &groups, &ctx).unwrap();
        assert_eq!(out.per_series["pt-a"].findings.len(), 1);
        assert!(out.per_series["pt-b"].findings.is_empty());
        assert!(Registry::default_multi_configs().iter().any(|c| c.id == ID));
    }
}
