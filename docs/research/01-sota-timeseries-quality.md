# Research 01 — State of the art in time-series data quality (generic)

*Research date: 2026-09-21. Every URL was returned by search or fetched; where a page could
not be opened this is stated. This is background for the check catalogue in
`docs/checks/catalogue.md`.*

## 1. Taxonomies and dimensions

**Generic standards.** ISO/IEC 25012 defines 15 characteristics split into *inherent*
(accuracy, completeness, consistency, credibility, currentness) and *system-dependent*
(availability, portability, recoverability, ...) ([ISO](https://www.iso.org/standard/35736.html),
[arc42 summary](https://quality.arc42.org/standards/iso-iec-25012)). ISO 8000 is about
governance and exchange of quality data rather than a dimension list
([arc42](https://quality.arc42.org/standards/iso-8000)). A 2025 MDPI review compares 12
frameworks and finds accuracy, completeness, consistency and timeliness are the only
dimensions present in nearly all ([MDPI BDCC 9(4):93](https://www.mdpi.com/2504-2289/9/4/93)).
A 2024 survey of DQ dimensions and tools for ML is the best cross-tool map
([arXiv 2406.19614](https://arxiv.org/html/2406.19614v1)); a 2025 practitioner survey adds how
dimensions are actually used ([arXiv 2507.17507](https://arxiv.org/html/2507.17507v1)).

**Sensor-specific taxonomies.** Teh et al. 2020, *Sensor data quality: a systematic review*
(57 papers): error types are outliers, bias, drift, missing values, constant/stuck values and
uncertainty; 56% of papers only detect, do not repair
([J. Big Data](https://journalofbigdata.springeropen.com/articles/10.1186/s40537-020-0285-1)).
Zhang, Jeong & Lee 2021 (IoT DQ management) list 8 error types: anomalies, missing,
deviations, drift, noise, constant values, uncertainty, stuck-at-zero; dimensions include
accuracy, completeness, timeliness, consistency, confidence, volume
([PMC8434542](https://pmc.ncbi.nlm.nih.gov/articles/PMC8434542/)). The 2025
privacy-preserving IoT time-series DQ paper is the most concrete: six operational metrics
with formulas — inter-arrival-time regularity (timeliness), IAT outliers via modified
z-score |0.6745(x−median)/MAD| > 3.5 (consistency), duplicates (uniqueness), missing
mandatory attributes (completeness), unknown attributes and format errors (validity), each
scaled 0–1 for weighted aggregation ([arXiv 2501.07154](https://arxiv.org/html/2501.07154)).
WATERVERSE (water-sector TS) scores completeness, consistency, timeliness, uniqueness,
validity ([arXiv 2403.03661](https://arxiv.org/abs/2403.03661)). Completeness is commonly
defined as the ratio of *originally measured, non-interpolated* values in a window, which
matters for historians that interpolate.

**Industrial practice view.** Mueller 2025, *Open Challenges in TSAD: An Industry
Perspective*, argues academic anomaly detection ignores streaming, human-in-the-loop,
conditional anomalies and population (fleet) analysis
([arXiv 2502.05392](https://arxiv.org/abs/2502.05392)).

## 2. Ranked list of ~40 generic checks

Ranking reflects value for process/energy historian data (frequency of occurrence ×
downstream damage × ease of automation). Thresholds are starting points; the recommended
pattern is to learn per-tag baselines from a 2–4 week reference window with robust
statistics (median/MAD, percentiles), then let users override.

### Tier A — structural, timestamp, flags (cheap, high yield)

1. **Gaps / missing samples.** Compare inter-arrival times (IAT) to expected period (mode of
   IAT); flag IAT > k×period. Default k=3 for regularly sampled tags; report completeness =
   actual/expected points. ([arXiv 2501.07154](https://arxiv.org/html/2501.07154))
2. **Staleness / last-update age.** now − last timestamp > tolerance. Default 3× expected
   period, min 5 min; TrendMiner marks tags "Delayed" after 4 h
   ([TrendMiner](https://documentation.trendminer.com/en/index-manager-and-performance-overview.html)).
3. **Flatline / stuck-at.** Run length of identical values (or |Δ| < resolution) > N samples;
   AVEVA recommends AF `Range()==0` over a window
   ([PI Square](https://community.aveva.com/pi-square-community/f/forum/86307/identifying-truly-stale-pi-tags-despite-updating-timestamps)).
   Default N = max(10 samples, 1 h); must be compression-aware (see #21) and allow legitimate
   constants (setpoints, digital tags).
4. **Bad/uncertain quality-flag ratio.** OPC DA/UA 2-bit quality (Good/Bad/Uncertain) +
   substatus (stale, sensor failure, comm loss, out of service); flag Bad >1% or Uncertain
   >5% per window ([OPC UA Part 8](https://reference.opcfoundation.org/v104/Core/docs/Part8/A.4.3/),
   [Canary quality codes](https://helpcenter.canarylabs.com/t/60hvl6y/understanding-quality-codes-version-23),
   [Cognite status codes](https://docs.cognite.com/dev/concepts/reference/status_codes)).
5. **Frozen quality flag.** Quality stays "Good" while value is stuck or IAT collapses
   (inconsistency between #3/#1 and #4).
6. **Duplicate timestamps.** Same ts, same value (drop) vs same ts, different value (flag,
   needs adjudication) ([arXiv 2501.12720](https://arxiv.org/pdf/2501.12720)). Default: any
   conflicting duplicate is an error.
7. **Out-of-order / non-monotonic timestamps.** Δt < 0; flag any occurrence
   (cf. GE `expect_column_values_to_be_increasing`).
8. **Irregular sampling / jitter.** IAT regularity score via relative error to modal IAT; warn
   if <0.9 ([arXiv 2501.07154](https://arxiv.org/html/2501.07154)).
9. **DST / timezone artefacts.** Detect 1 h gap or 1 h duplicate block on DST boundaries;
   naive-timestamp ambiguity ([Streamkap](https://streamkap.com/resources-and-guides/timestamp-handling-streaming)).
10. **Clock skew.** Timestamps in the future or systematically ahead/behind a reference tag;
    flag ts > ingest time + 1 min.
11. **NaN / Inf / non-numeric / type change.** Count and ratio; any Inf is an error, NaN ratio
    >1% warns.
12. **Late arrival / backfill.** ingest time − event time > SLA; default SLA = 2× period.

### Tier B — value validity

13. **Physical-limit range.** Hard limits from engineering units (0–100 %, T > 0 K); any
    violation is an error. Timeseer calls these "improbable limits"
    ([element61](https://www.element61.be/en/resource/ensuring-time-series-data-quality-timeseerai)).
14. **Operational-limit range.** Learned p0.1/p99.9 (or median ± 5·MAD) of reference window;
    warn on >0.5% out-of-band.
15. **Negative values for non-negative quantities.** Flow, level, concentration, power (where
    non-reversible); any negative beyond −resolution is an error.
16. **Unit/scale error.** Sudden ×10/×1000 or °C↔°F shifts: compare window median to
    reference median; ratio in {≈10, ≈1000, ≈1.8x+32} → error.
17. **Spikes (point outliers).** Hampel filter: rolling median ± t·1.4826·MAD, window 5–25
    samples, t=3 ([pracma](https://search.r-project.org/CRAN/refmans/pracma/html/hampel.html),
    [MATLAB](https://www.mathworks.com/help/signal/ref/hampel.html)); modified z-score >3.5;
    IQR 1.5×; Seasonal-Hybrid ESD when strong periodicity (STL residual + generalized ESD,
    α=0.05, max anomalies 1–10%) ([Twitter S-H-ESD](https://blog.x.com/engineering/en_us/a/2015/introducing-practical-and-robust-anomaly-detection-in-a-time-series));
    Isolation Forest (contamination 0.5–1%) for multivariate.
18. **Rate-of-change violation.** |Δx/Δt| > max slew rate; default learned p99.9 of |Δx/Δt| × 3,
    or physical limit if known.
19. **Excessive noise / variance jump.** Rolling std or MAD vs reference; flag ratio >2
    (noisier) or <0.3 (suspiciously smooth/filtered).
20. **Quantization / resolution loss.** Count distinct values and smallest non-zero |Δ| in a
    window; flag if unique-value count drops (<10 where historically >100) or step size grows.
21. **Compression artefacts (deadband / swinging door).** PI ExcDev/CompDev too large removes
    detail: long linear segments, IAT far larger than scan rate, "noise-free" signals.
    Faqehi et al. 2025 quantify how compression skews statistics and ML accuracy
    ([arXiv 2510.26868](https://arxiv.org/abs/2510.26868)); AVEVA 2023 guidance, common rule
    ExcDev ≈ ½ CompDev ([AVEVA UC23](https://cdn.osisoft.com/osi/presentations/2023-AVEVA-San-Francisco/UC23NA-3PGK04-AVEVA_Bregenzer_Brent-Exception-Compression-and-their-Impacts-On-PI-System-Performance.pdf),
    [PiSharp](https://www.pisharp.com/article/359/understanding-exception-and-compression-in-pi-data-archive)).
22. **Digital/state tags: illegal states and chattering.** Value outside enumerated set; state
    toggles > N per minute.
23. **Interpolated vs raw ratio.** If the historian marks interpolated/calculated points
    (OPC HDA), completeness counts raw only.

### Tier C — drift, distribution, dynamics

24. **Level drift / bias.** Slope of rolling median over days (Theil–Sen), or CUSUM /
    Page–Hinkley on residuals (river defaults δ≈0.005·σ, λ≈50; verify per version)
    ([stream survey](https://arxiv.org/pdf/2204.13625)).
25. **Distribution drift vs reference.** Evidently defaults: KS (p<0.05) for n≤1000,
    normalized Wasserstein ≥0.1 for n>1000; PSI ≥0.1 warn / ≥0.25 alert
    ([Evidently](https://docs-old.evidentlyai.com/user-guide/customization/options-for-statistical-tests),
    [PSI](https://www.risk.net/journal-of-risk-model-validation/7725371/statistical-properties-of-the-population-stability-index));
    WhyLabs recommends Hellinger ([WhyLabs](https://docs.whylabs.ai/docs/drift-algorithms/)).
26. **Online change detection.** ADWIN (δ=0.002) for streaming mean shift.
27. **Offline changepoints.** PELT with `rbf`/`l2` cost, BIC penalty (log n·σ²) in `ruptures`
    ([docs](https://centre-borelli.github.io/ruptures-docs/user-guide/detection/pelt/),
    [arXiv 1801.00826](https://arxiv.org/pdf/1801.00826)); BOCPD (Adams & MacKay) for
    probabilistic online use.
28. **Seasonality / periodicity break.** STL on reference period; flag if dominant period
    (ACF/FFT) changes or seasonal strength drops >50%.
29. **Autocorrelation / dynamics change.** Lag-1 ACF or spectral entropy shift vs reference;
    catches filtered vs raw feeds.
30. **Forecast-residual anomaly.** Residual of a simple forecaster (ETS/ARIMA or a foundation
    model like Chronos/TimesFM/TimeGPT) outside 99% PI
    ([Nixtla](https://www.nixtla.io/docs/anomaly_detection/real-time/introduction),
    [ChronosAD](https://arxiv.org/pdf/2606.01300)). TSB-AD (NeurIPS 2024) found foundation
    models strong on *point* anomalies but simple statistical methods competitive overall
    ([TSB-AD](https://github.com/TheDatumOrg/TSB-AD)).
31. **Subsequence/pattern anomalies.** Matrix Profile discord or LSTM-AE; Schmidl et al. 2022
    (71 algorithms, 976 series) find no universal winner; simple methods often best
    ([VLDB 2022](https://www.vldb.org/pvldb/vol15/p1779-wenig.pdf)).

### Tier D — cross-tag consistency

32. **Broken correlation.** Rolling Pearson/Spearman between paired tags vs reference; flag
    |ρ_now − ρ_ref| > 0.3 or sign flip ([Li 2022](https://pmc.ncbi.nlm.nih.gov/articles/PMC9173954/)).
33. **Redundant-sensor disagreement.** |A − B| > tolerance (2× combined accuracy) for ≥ N min;
    median-of-three voting.
34. **Soft-sensor / regression residual.** Predict tag from related tags (PLS/ridge);
    residual > 3·MAD → sensor drift.
35. **Mass / energy balance residual.** Σin − Σout normalized; gross-error detection via global
    χ² test and measurement test from data-reconciliation literature
    ([review](https://www.researchgate.net/publication/356715521_A_Review_on_Data_Reconciliation_and_Gross_Error_Detection_for_Process_Plant_Energy_Management),
    [benchmark set](https://www.sciencedirect.com/science/article/abs/pii/S0098135418300024)).
    Default: |residual| > 3σ_reconciled.
36. **Lag / synchronization drift.** Cross-correlation lag between physically coupled tags
    shifts by > 1 sample.
37. **Calibration-event detection.** Step change plus temporary flat/zero and Bad quality;
    correlate with maintenance logs; exclude from drift baselines.
38. **Fleet / population outlier.** Tag's summary stats vs peers of the same template
    (AF template, equipment class): robust z > 3.5.
39. **Metadata validity.** Missing engineering units, limits, description; unit mismatch with
    value magnitude.
40. **Volume/row-count anomaly per batch.** Deequ `RelativeRateOfChangeStrategy`
    (maxRateIncrease 2.0) on daily counts
    ([Deequ](https://github.com/awslabs/deequ/blob/master/src/main/scala/com/amazon/deequ/examples/anomaly_detection_example.md)).

## 3. Tools vs coverage

| Tool | Type | TS-specific checks | Notes |
|---|---|---|---|
| Timeseer.ai | Commercial (industrial) | Yes: >100 scores in ~20 KPIs: stale, flatline, missing/sampling-rate, improbable limits, variance drift, sensor drift, broken correlations; auto-learned per-tag thresholds; weighted KPI dashboards | docs.timeseer.ai did not resolve; details from [element61](https://www.element61.be/en/resource/ensuring-time-series-data-quality-timeseerai), [product page](https://www.timeseer.ai/platform) |
| Cognite Data Fusion | Commercial | Partial: SHACL/SPARQL rule package validates TS datapoints (too few points, gaps, out-of-range, stale) with severity | [docs](https://cognite-data-quality-validation.readthedocs-hosted.com/en/latest/usage/time_series_mode/index.html) |
| AVEVA PI (AF Analytics) | Commercial | DIY: `Range()`, `BadVal`, event frames; exception/compression tuning is the main lever | [PI Square](https://community.aveva.com/pi-square-community/f/forum/86307/identifying-truly-stale-pi-tags-despite-updating-timestamps) |
| Seeq | Commercial | Cleansing, not scoring: `removeOutliers()`, `agileFilter()`, `validValues()`, IQR recipes | [Seeq](https://www.seeq.org/topic/2137-data-cleansing-tips-using-the-remove-and-within-formula-functions/) |
| TrendMiner | Commercial | Tag index status (Delayed >4 h), monitors on saved searches | [guide](https://userguide.trendminer.com/2025.R3.0/en/monitoring-and-alert-overview.html) |
| Canary Labs | Commercial | Quality codes (OPC + NoData, audit bits); no scoring | [Canary](https://helpcenter.canarylabs.com/t/60hvl6y/understanding-quality-codes-version-23) |
| Palantir Foundry | Commercial | Tabular health checks (freshness, size, schema); no TS-native checks | [Palantir](https://www.palantir.com/docs/foundry/data-health/overview) |
| Monte Carlo / Bigeye / Anomalo | Commercial observability | Metric TS (freshness, volume, distribution) with ML thresholds; not sensor-level | [comparison](https://pipecode.ai/blogs/data-observability-monte-carlo-anomalo-bigeye-lightup) |
| Great Expectations | OSS | Tabular; z-score, increasing; separate `great-expectations-time-series-expectations` | [issue](https://github.com/great-expectations/great_expectations/issues/1183), [PyPI](https://pypi.org/project/great-expectations-time-series-expectations) |
| Soda Core | OSS | Prophet anomaly-score check deprecated in favour of Metric Monitoring | [Soda](https://docs.soda.io/soda-documentation/soda-v3/sodacl-reference/anomaly-score) |
| Deequ/PyDeequ | OSS | Metric-history anomaly strategies | [Deequ](https://github.com/awslabs/deequ) |
| Evidently | OSS | Drift tests with defaults (KS/Wasserstein/PSI/JS) | [Evidently](https://docs-old.evidentlyai.com/user-guide/customization/options-for-statistical-tests) |
| whylogs / WhyLabs | OSS/SaaS | Profile drift (Hellinger) | [WhyLabs](https://docs.whylabs.ai/docs/drift-algorithms/) |
| ydata-profiling | OSS | `tsmode=True`: ADF stationarity, seasonality, ACF/PACF alerts | [docs](https://docs.profiling.ydata.ai/latest/features/time_series_datasets/) |
| pandera | OSS | Schema/dtype/range; no TS-native | [pandera](https://pandera.readthedocs.io/en/stable/checks.html) |
| timeseries-qc (PyPI) | OSS | Null, flatline, delta, range, outlier rules, timestamp health, YAML, HTML | [PyPI](https://pypi.org/project/timeseries-qc/0.4.1/) |
| pyod / Merlion / Kats / Darts / sktime | OSS | Anomaly detectors; Merlion has post-processing and AutoML | [Merlion](https://github.com/salesforce/Merlion) |
| river | OSS | Online drift: ADWIN, Page-Hinkley, KSWIN | [survey](https://arxiv.org/pdf/2204.13625) |
| ruptures | OSS | PELT, BinSeg, BottomUp, Window, kernel CPD | [ruptures](https://centre-borelli.github.io/ruptures-docs/) |
| Nixtla TimeGPT | SaaS | `detect_anomalies_online` via forecast error | [Nixtla](https://www.nixtla.io/docs/anomaly_detection/real-time/introduction) |
| Benchmarks | – | UCR Anomaly Archive ([Wu & Keogh](https://arxiv.org/abs/2009.13807)), TSB-UAD/TSB-AD ([GitHub](https://github.com/TheDatumOrg/TSB-AD)), TimeEval ([VLDB 2022](https://www.vldb.org/pvldb/vol15/p3678-schmidl.pdf)), TAB ([arXiv 2506.18046](https://arxiv.org/html/2506.18046v1)), TimeSeriesBench ([arXiv 2402.10802](https://arxiv.org/pdf/2402.10802)) | NAB and Yahoo criticised for triviality, unrealistic density, mislabels |

Only Timeseer (fully), Cognite (partially) and timeseries-qc ship sensor-level time-series
checks; everything else is tabular DQ, cleansing, or anomaly detection without DQ semantics.

## 4. Scoring and thresholds

- **Common pattern:** each check yields a 0–1 pass ratio; dimension score = weighted mean of
  its checks; overall = weighted mean of dimensions, shown as traffic light. DQSOps (2023)
  standardizes per-metric scores before weighted averaging
  ([ACM](https://dl.acm.org/doi/fullHtml/10.1145/3593434.3593445)); its 2024 successor adds a
  drift-aware change detector so reference statistics re-baseline when the process
  legitimately changes ([arXiv 2408.06724](https://arxiv.org/abs/2408.06724)). The 2025 IoT
  paper's per-metric 1 − error_ratio design is the cleanest published template.
- **Threshold setting:** (a) physical limits from metadata; (b) robust learned bands
  (median ± k·1.4826·MAD, k=3–3.5, or p0.5/p99.5) from a reference window after removing
  Bad-quality points; (c) statistical defaults for drift (PSI 0.1/0.25, KS p 0.05,
  Wasserstein 0.1); (d) seasonal-aware models when periodicity is strong; (e) re-baseline on
  confirmed changepoints. Score severity by *fraction of window affected* and *distance
  beyond band*, not binary pass/fail.

## 5. Explainability and repair

- **Explainability:** 2024–2026 work moves toward conditional attribution (explain relative
  to similar normal states) ([arXiv 2604.17616](https://arxiv.org/abs/2604.17616)),
  counterfactuals for AE-based TSAD ([arXiv 2501.02069](https://arxiv.org/pdf/2501.02069)) and
  causally-guided transformers ([arXiv 2604.17998](https://arxiv.org/html/2604.17998v1)).
  Rule-based checks are inherently explainable; the gap is explaining *learned* thresholds.
- **Repair/imputation:** TSI-Bench (2024, 28 methods, 8 datasets) on the PyPOTS stack: SAITS
  (self-attention) is fast and strong; BRITS good but slow; CSDI (diffusion) best on some
  metrics but very slow ([arXiv 2406.12747](https://arxiv.org/pdf/2406.12747),
  [PyPOTS](https://pypots.com/pubs/), [SAITS](https://github.com/WenjieDu/SAITS)). For
  univariate industrial tags, Kalman smoothing on a structural model and seasonal
  decomposition + interpolation usually beat linear interpolation when trend/seasonality
  exist; linear is fine for short gaps ([imputeTS](https://steffenmoritz.github.io/imputeTS/reference/na_kalman.html),
  [Moritz](https://arxiv.org/pdf/1510.03924)). Mass-balance data reconciliation is the
  domain-native repair for flows/energy.

## 5b. Correction and repair as a product capability (added 2026-09-21)

The generic tools in §3 only *detect*. Two commercial products make correction a
first-class workflow, from public material only:

| Product | What is public | Notes |
|---|---|---|
| Timeseer.AI "Resolve" and flows | Platform is framed as Detect → Validate → Resolve. Resolve: "fix the sensor, clean the data, or confidently accept the data"; manual cleaning by selecting a period on the chart; fully automated cleaning on a schedule; incident grouping and follow-up actions; "full visibility into every change and decision". Configurable **flows** chain blocks (data analysis, filter, imputation, alignment) and the final block publishes the cleaned data to a data service as a new, reliable copy consumed downstream (e.g. Databricks) ([timeseer.ai/platform](https://www.timeseer.ai/platform), [element61 case](https://www.element61.be/en/resource/ensuring-time-series-data-quality-timeseerai)). | No public product called "Seer" was found; block internals and algorithms are not documented publicly. |
| Seeq | Formula-based cleansing (`remove()`, `within()`, `agileFilter()`, `removeOutliers()`) producing derived signals; no scoring, no write-back workflow. | Cleansing lives in the analyst's workbook, not as governed data. |
| MDM systems (Oracle, Itron, L+G) | VEE "Editing": estimation with substitution codes, manual edits with audit, resubmission to settlement. | Metering only; strongest audit model of the three. |

Academic repair (§5) and product repair differ in what matters: products need **period
selection, a small set of understandable operations, lineage per point, approval, and a
published corrected copy**, not the best imputation MAE.

### Gap analysis addendum

10. **Corrections without a version model.** Public material shows corrections produce a
    new dataset copy; none describes per-point lineage (which check, which method, who
    approved), reversibility, or a diff between raw and corrected. MDM substitution codes are
    the closest prior art and only exist in metering.
11. **Corrections that feed back into detection.** No tool documents re-scoring a corrected
    series against the raw one or using accepted corrections to tune thresholds.
12. **Physics-aware repair** (balance-preserving imputation, quality-flag-aware estimation)
    remains unaddressed (see gap 7).

## 6. Gap analysis — what nobody does well yet

1. **Compression-aware checks.** No OSS tool models historian exception/compression settings,
   so flatline, noise, quantization and completeness checks give false positives on PI/Canary
   data.
2. **Semantics of "constant".** Distinguishing stuck sensors from legitimately constant tags
   (setpoints, off-state) needs tag-type context; only Timeseer claims auto-learning.
3. **Quality flags as first-class input.** OPC quality codes, interpolated/calculated markers
   and audit bits are rarely consumed by DQ tools or benchmarks.
4. **Conditional / context-dependent validity** (operating mode, batch phase, start-up):
   range and drift checks without mode segmentation are the largest false-positive source.
5. **Fleet-level baselines.** Population/template-based thresholds are almost absent.
6. **Benchmarks for DQ, not anomalies.** No public labelled corpus of industrial
   stale/spike/compression/unit-error episodes exists.
7. **Repair that respects physics.** Deep imputers ignore balances and quality flags; data
   reconciliation ignores temporal dynamics; nobody combines both.
8. **Score semantics.** Weighted averages hide which check failed and lack calibration.
9. **Explainable learned thresholds and human feedback loops** (accept/ignore → threshold
   update) are missing from every OSS tool surveyed.

**Unverified / could not open:** docs.timeseer.ai (DNS failure), a Medium article on stale
detection (403), Deequ strategies beyond RelativeRateOfChange, exact river default
parameters, tsfresh/TSFEL/Elementary details.

## Implications for Tabayyun

The twelve gaps above (nine generic, three on correction) are the product's differentiation list. In particular: compression-aware
checks, quality flags as input, operating-mode segmentation, fleet baselines, a labelled DQ
benchmark corpus (which we should build and publish), a feedback loop from user
triage to thresholds, and corrections modelled as versioned, lineage-carrying layers rather than
overwritten copies.
