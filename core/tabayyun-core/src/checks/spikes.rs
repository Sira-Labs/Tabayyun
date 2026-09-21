//! `tby.spikes` — point outliers via a Hampel filter (catalogue #13).
//!
//! A Hampel hit is only a spike when it is isolated: a run of at most `max_width`
//! consecutive hits bounded by ordinary samples. Longer runs are excursions (appliance
//! switching, a solar ramp against a night-time median) and are left to the rate-of-change,
//! operational-range and changepoint checks. Spikes close together form one finding per
//! cluster; when a series has more clusters than `max_findings`, spikes are a property of
//! the signal rather than isolated faults and a single summary finding is emitted (ADR-0011).

use super::{duration_param, expected_interval, metric, Check, CheckContext, CheckOutput};
use crate::error::Result;
use crate::finding::{Dimension, Finding, Severity, Window};
use crate::frame::SeriesFrame;
use crate::profile::{quantile_f64, resolution, Profile};
use crate::time::{format_duration, NS_PER_HOUR};
use serde::{Deserialize, Serialize};

pub const ID: &str = "tby.spikes";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Spikes {
    /// Threshold in robust standard deviations (1.4826 × MAD).
    pub t: f64,
    /// Centred rolling window length in samples (odd).
    pub window: usize,
    /// Floor for the scaled MAD so a locally constant signal does not flag tiny changes;
    /// `None` = series resolution (or the baseline noise floor if larger).
    pub min_scale: Option<f64>,
    /// Longest run of consecutive Hampel hits that still counts as a spike; longer runs are
    /// excursions, not point outliers.
    pub max_width: usize,
    /// Spikes closer than this form one cluster finding ("auto" = max(1 h, 12 × interval)).
    pub cluster_gap: String,
    /// More clusters than this collapse into one summary finding for the whole window.
    pub max_findings: usize,
    /// Timestamps listed per cluster finding (the count is always exact).
    pub max_listed: usize,
    pub severity: Severity,
}

impl Default for Spikes {
    fn default() -> Self {
        Self {
            t: 4.0,
            window: 21,
            min_scale: None,
            max_width: 3,
            cluster_gap: "auto".into(),
            max_findings: 20,
            max_listed: 10,
            severity: Severity::Medium,
        }
    }
}

/// One Hampel hit: sample index, value, local median, robust z.
#[derive(Debug, Clone, Copy)]
struct Hit {
    i: usize,
    x: f64,
    med: f64,
    z: f64,
}

/// Spikes within `gap` of each other, in time order.
#[derive(Debug, Clone)]
struct Cluster {
    spikes: Vec<Hit>,
}

impl Cluster {
    fn peak(&self) -> &Hit {
        self.spikes.iter().max_by(|a, b| a.z.total_cmp(&b.z)).expect("cluster is never empty")
    }
}

impl Check for Spikes {
    fn id(&self) -> &'static str {
        ID
    }
    fn dimension(&self) -> Dimension {
        Dimension::Plausibility
    }
    fn default_severity(&self) -> Severity {
        self.severity
    }

    fn run(&self, frame: &SeriesFrame, ctx: &CheckContext) -> Result<CheckOutput> {
        let mut out = CheckOutput::default();
        let (f, _) = frame.normalized();
        let n = f.len();
        let w = self.window.max(3) | 1;
        if n < w {
            return Ok(out);
        }
        let interval = expected_interval(&f, ctx).unwrap_or(1).max(1);
        let cluster_gap = if self.cluster_gap == "auto" {
            (12 * interval).max(NS_PER_HOUR)
        } else {
            duration_param(ID, "cluster_gap", &self.cluster_gap)?
        };
        // Scale floor: the series' robust noise sigma or its resolution, whichever is larger.
        // A 21-sample MAD underestimates the scale often enough that, without the floor,
        // ordinary noise is flagged at a few tenths of a percent.
        let floor = self.min_scale.unwrap_or_else(|| {
            let res = frame.meta.resolution.or_else(|| resolution(&f.values)).unwrap_or(0.0);
            let noise = match &ctx.profile {
                Some(p) => p.noise_mad,
                None => Profile::compute(&f).noise_mad,
            }
            .unwrap_or(0.0);
            res.max(noise)
        });
        let half = w / 2;
        let mut buf: Vec<f64> = Vec::with_capacity(w);
        let mut hits: Vec<Hit> = Vec::new();
        for i in 0..n {
            let x = f.values[i];
            if !x.is_finite() || !f.quality[i].is_usable() {
                continue;
            }
            let lo = i.saturating_sub(half);
            let hi = (i + half + 1).min(n);
            buf.clear();
            buf.extend(f.values[lo..hi].iter().copied().filter(|v| v.is_finite()));
            if buf.len() < 3 {
                continue;
            }
            buf.sort_by(|a, b| a.partial_cmp(b).unwrap());
            let med = quantile_f64(&buf, 0.5);
            let mut dev: Vec<f64> = buf.iter().map(|v| (v - med).abs()).collect();
            dev.sort_by(|a, b| a.partial_cmp(b).unwrap());
            let scale = (1.4826 * quantile_f64(&dev, 0.5)).max(floor);
            if scale <= 0.0 {
                continue;
            }
            let z = (x - med).abs() / scale;
            if z > self.t {
                hits.push(Hit { i, x, med, z });
            }
        }
        // Isolated hits are spikes; runs of consecutive hits longer than max_width are
        // excursions and belong to other checks.
        let mut spikes: Vec<Hit> = Vec::new();
        let mut excursions = 0usize;
        let mut k = 0;
        while k < hits.len() {
            let mut e = k + 1;
            while e < hits.len() && hits[e].i == hits[e - 1].i + 1 {
                e += 1;
            }
            if e - k <= self.max_width.max(1) {
                spikes.extend_from_slice(&hits[k..e]);
            } else {
                excursions += 1;
            }
            k = e;
        }
        let mut clusters: Vec<Cluster> = Vec::new();
        for h in &spikes {
            match clusters.last_mut() {
                Some(c) if f.ts[h.i] - f.ts[c.spikes.last().unwrap().i] <= cluster_gap => c.spikes.push(*h),
                _ => clusters.push(Cluster { spikes: vec![*h] }),
            }
        }
        out.metrics.push(metric(ID, &f, "spike_count", ctx.window.end, spikes.len() as f64));
        out.metrics.push(metric(ID, &f, "spike_clusters", ctx.window.end, clusters.len() as f64));
        out.metrics.push(metric(ID, &f, "excursion_runs", ctx.window.end, excursions as f64));
        if clusters.is_empty() {
            return Ok(out);
        }
        let cluster_json = |c: &Cluster| {
            let p = c.peak();
            serde_json::json!({
                "start_ts": f.ts[c.spikes[0].i], "end_ts": f.ts[c.spikes.last().unwrap().i] + interval,
                "count": c.spikes.len(), "peak_ts": f.ts[p.i], "peak_value": p.x, "peak_local_median": p.med, "peak_z": p.z,
                "ts": c.spikes.iter().take(self.max_listed).map(|h| f.ts[h.i]).collect::<Vec<_>>(),
                "values": c.spikes.iter().take(self.max_listed).map(|h| h.x).collect::<Vec<_>>(),
            })
        };
        if clusters.len() > self.max_findings {
            // Spikes are a property of this signal; one summary keeps the list actionable.
            let mut top: Vec<&Cluster> = clusters.iter().collect();
            top.sort_by(|a, b| b.spikes.len().cmp(&a.spikes.len()).then(b.peak().z.total_cmp(&a.peak().z)));
            let win = Window::new(
                f.ts[clusters[0].spikes[0].i],
                f.ts[clusters.last().unwrap().spikes.last().unwrap().i] + interval,
            );
            let peak = clusters.iter().map(|c| c.peak()).max_by(|a, b| a.z.total_cmp(&b.z)).unwrap();
            out.findings.push(Finding::new(
                ID, &f.meta.id, Dimension::Plausibility, Severity::Low, win,
                spikes.len() as f64 / n as f64,
                format!(
                    "Spiky signal: {} spikes in {} clusters over {} ({:.2} % of samples; largest {:.1} robust standard deviations); clusters are not listed individually",
                    spikes.len(), clusters.len(), format_duration(win.duration()), 100.0 * spikes.len() as f64 / n as f64, peak.z
                ),
                serde_json::json!({"summary_of": clusters.len(), "spike_count": spikes.len(), "spike_fraction": spikes.len() as f64 / n as f64,
                    "peak_ts": f.ts[peak.i], "peak_value": peak.x, "peak_z": peak.z, "t": self.t, "window": w, "max_width": self.max_width,
                    "cluster_gap_ns": cluster_gap, "max_findings": self.max_findings,
                    "largest_clusters": top.iter().take(self.max_listed).map(|c| cluster_json(c)).collect::<Vec<_>>()}),
            ));
            return Ok(out);
        }
        for c in &clusters {
            let first = c.spikes[0];
            let last = c.spikes.last().unwrap();
            let win = Window::new(f.ts[first.i], f.ts[last.i] + interval);
            let p = c.peak();
            let summary = if c.spikes.len() == 1 {
                format!(
                    "Spike: value {:.4} is {:.1} robust standard deviations from the local median {:.4}",
                    p.x, p.z, p.med
                )
            } else {
                format!(
                    "{} spikes within {}: largest is value {:.4}, {:.1} robust standard deviations from the local median {:.4}",
                    c.spikes.len(), format_duration(win.duration()), p.x, p.z, p.med
                )
            };
            let mut evidence = cluster_json(c);
            evidence["t"] = serde_json::json!(self.t);
            evidence["window"] = serde_json::json!(w);
            evidence["max_width"] = serde_json::json!(self.max_width);
            evidence["cluster_gap_ns"] = serde_json::json!(cluster_gap);
            out.findings.push(Finding::new(
                ID,
                &f.meta.id,
                Dimension::Plausibility,
                self.severity,
                win,
                c.spikes.len() as f64 / n as f64,
                summary,
                evidence,
            ));
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::checks::testutil::*;
    use crate::synth::{inject, Rng};

    /// Every spike timestamp listed across cluster findings.
    fn listed_ts(out: &CheckOutput) -> Vec<i64> {
        ids(out, ID)
            .iter()
            .flat_map(|x| x.evidence["ts"].as_array().unwrap().iter().map(|v| v.as_i64().unwrap()))
            .collect()
    }

    #[test]
    fn finds_injected_spikes_and_little_else() {
        let mut f = base(2880);
        let mut rng = Rng::new(3);
        let idx = inject::spikes(&mut f, &mut rng, 5, 10.0);
        let out = Spikes::default().run(&f, &ctx(&f)).unwrap();
        let found = listed_ts(&out);
        for i in idx {
            assert!(found.contains(&f.ts[i]), "spike at {i} not found");
        }
        // Gaussian noise at 3 robust sigma yields a small false-positive rate; keep it bounded.
        assert!(found.len() <= 5 + 2880 / 500, "too many spikes: {}", found.len());
        assert!(ids(&out, ID).iter().all(|x| x.severity == Severity::Medium));
    }

    #[test]
    fn constant_signal_with_one_step_is_not_a_spike_storm() {
        let mut f = base(500);
        inject::flatline(&mut f, 0, 500);
        f.values[250] += 0.001; // below the resolution floor once resolution is derived
        let out = Spikes::default().run(&f, &ctx(&f)).unwrap();
        assert!(ids(&out, ID).len() <= 1);
    }

    #[test]
    fn burst_of_spikes_is_one_finding_with_count() {
        let mut f = base(2880);
        for k in 0..4 {
            f.values[1000 + 7 * k] += 12.0; // four spikes 7 min apart, well inside one hour
        }
        let out = Spikes::default().run(&f, &ctx(&f)).unwrap();
        let fs = ids(&out, ID);
        assert_eq!(fs.len(), 1, "{:?}", out.findings);
        assert_eq!(fs[0].evidence["count"], 4);
        assert_eq!(fs[0].window.start, f.ts[1000]);
        assert!(fs[0].summary.starts_with("4 spikes within"));
    }

    #[test]
    fn excursion_is_not_a_spike() {
        let mut f = base(2880);
        inject::set(&mut f, 1000, 8, 90.0); // 8-minute step well above the local median
        f.values[2000] += 12.0; // one genuine spike
        let out = Spikes::default().run(&f, &ctx(&f)).unwrap();
        let listed = listed_ts(&out);
        assert!(listed.contains(&f.ts[2000]));
        assert!(!listed.contains(&f.ts[1004]), "{:?}", out.findings);
        let excursions = out.metrics.iter().find(|m| m.name == "excursion_runs").unwrap().value;
        assert_eq!(excursions, 1.0);
    }

    #[test]
    fn spiky_signal_collapses_to_one_summary() {
        let mut f = base(20 * 1440);
        let mut rng = Rng::new(7);
        inject::spikes(&mut f, &mut rng, 400, 12.0); // ~1.4 % of samples, spread over 20 days
        let out = Spikes::default().run(&f, &ctx(&f)).unwrap();
        let fs = ids(&out, ID);
        assert_eq!(fs.len(), 1, "{:?}", out.findings.len());
        assert_eq!(fs[0].severity, Severity::Low);
        assert!(fs[0].evidence["summary_of"].as_u64().unwrap() > 20);
        assert!(fs[0].evidence["spike_count"].as_u64().unwrap() >= 380);
        assert_eq!(fs[0].evidence["largest_clusters"].as_array().unwrap().len(), 10);
    }
}
