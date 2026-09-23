//! Tabayyun core: time-series data-quality frames, profiling, checks and scoring.
//!
//! Design (see `docs/architecture/03-system-architecture.md`, ADR-0001, ADR-0015):
//! - A [`SeriesFrame`] is one time series with metadata, timestamps (ns since Unix epoch,
//!   UTC), `f64` values (NaN = null) and a normalised [`Quality`] per sample.
//! - A [`Check`] is pure: it receives a frame plus a [`CheckContext`] and returns
//!   [`Finding`]s with machine-readable evidence and optional metrics. No I/O.
//! - A [`cross::CrossCheck`] runs on the members of a [`cross::SeriesGroup`], put on one
//!   grid by [`align`]; [`Registry::run_multi`] runs both kinds over several series (spec 008).
//! - [`score`] turns findings into per-dimension and overall scores.
//! - [`seasonal`] detects a series' dominant period and seasonal strength (spec 012).
//! - [`cache`] stores raw observations as Parquet on local disk or S3 (spec 006); it is the
//!   only module that does I/O.
//! - [`synth`] generates deterministic synthetic series with injected faults for tests,
//!   benchmarks and demos.
//!
//! Checks are sequential kernels over the frame's plain buffers, with Arrow only at the
//! boundary (ADR-0015).

pub mod align;
pub mod cache;
pub mod checks;
pub mod cross;
pub mod downsample;
pub mod error;
pub mod finding;
pub mod frame;
pub mod profile;
pub mod quality;
pub mod registry;
pub mod score;
pub mod seasonal;
pub mod synth;
pub mod time;

pub use checks::{Check, CheckContext, CheckOutput};
pub use cross::{CrossCheck, GroupKind, GroupMember, GroupSkip, MemberRole, SeriesGroup};
pub use error::{Error, Result};
pub use finding::{Dimension, Finding, Metric, Severity, Window};
pub use frame::{SeriesFrame, SeriesKind, SeriesMeta};
pub use profile::Profile;
pub use quality::Quality;
pub use registry::{CheckConfig, MultiOutput, Registry};
pub use score::{ScoreReport, Scorer};
