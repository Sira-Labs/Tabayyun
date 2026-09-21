# Research 02 — Time-series data quality in the energy sector

*Research date: 2026-09-21. Regulatory PDFs (AEMO, Elexon, MHHS, NAESB UBP, ERCOT, NASPI,
BSRN, ENTSO-E QoCDC, IEC previews) were downloaded and text-extracted to confirm thresholds.
Only URLs actually opened or returned by search are cited; items marked (search-only) were
not deep-read. Feeds the energy packs in `docs/checks/catalogue.md`.*

## 1. Smart metering / AMI (VEE: validation, estimation, editing)

**Data shapes.** Interval energy (kWh, kvarh) at 5/15/30 min (Australian NEM moved to
5-min; UK half-hourly = 30 min; Germany/most of EU = 15 min); one or a few channels per
meter point (import/export, active/reactive, TOU registers) but millions of meter points;
register reads (cumulative, roll over at e.g. 99999); alarms/status flags travel with the
data. Estimated vs actual data is flagged (Elexon "A"/"E", AEMO substitution types
11–21/51–58/61–69, MHHS "Estimation Reason Code").

| Check | Rule | Default threshold (published) | Source |
|---|---|---|---|
| Meter time tolerance / clock drift | Compare meter clock to reference; compare #intervals retrieved vs expected for elapsed time | UBP: fail if >3 min; 3 consecutive fails → physical inspection; prorate if drift ≤55 min, else estimate. Elexon: correct if 20 s–15 min off, investigate >15 min. ERCOT: clock ±2 min/week | [NAESB UBP §1.3.2](https://www.ercot.com/files/docs/2016/09/15/ubp120500.pdf); [BSCP502 App.4.1.3](https://www.elexon.co.uk/bsc/documents/bsc-codes/bscp502/attachment/bscp502/); [ERCOT SMOG §6.8.2](https://www.ercot.com/files/docs/2025/05/30/ERCOT-Settlement-Metering-Operating-Guide-June-1-2025.pdf) |
| Sum check (interval sum vs register advance, with rollover) | Σ intervals vs (stop − start register), handling rollover | UBP: pass if diff ≤ 2 meter multipliers. Elexon: ±0.7% weekly / ±5% daily "mini-MAR". MHHS: ±0.1% on site-read Meter Advance Reconciliation | UBP §1.4.3; BSCP502 App.4.1.5; [MHHS METH002 §2.16](https://www.mhhsprogramme.co.uk/api/documentlibrary/Design%20Documents/MHHSP-METH002_ADS_Validation_Estimation%20v5.7%20Redlined%20version.pdf) |
| Spike check | Per 24 h: (highest − 3rd highest)/3rd highest | UBP: fail if >1.8; skip if highest ≤10 pulses. Oracle MDM Interval Spike Check uses the same logic with configurable tolerance | UBP §1.4.4; [Oracle Interval Spike Check](https://docs.oracle.com/en/industries/energy-water/meter-data-management/2.5.0.0.0/mdm-user-guides/MDM_25000/D1_AG_VEE_IntervalSpikeCheck.html) |
| Maximum permissible energy / CT-ratio ceiling | Interval energy vs capacity of metering CoP or CT ratio | Elexon table (e.g. CoP1 400,000 kWh/HH; CoP5 600; CoP6/7 50); use actual if ≤20% over, estimate if >20%. AEMO: nominated max from CT ratio / meter rating | BSCP502 App.4.1.6; [AEMO Metrology Part B §10.2](https://www.aemo.com.au/-/media/files/stakeholder_consultation/consultations/nem-consultations/2020/retail-procedures-wdr/procedure-and-guidelines/metrology-procedure-part-b-v702-clean.pdf) |
| Negative consumption | Every import interval ≥ 0 | MHHS §2.25 (hard rule); Oracle "Negative Consumption Check" | MHHS; [Oracle VEE rules](https://docs.oracle.com/en/industries/energy-water/smart-grid-gateway/2.5.0.0.0/sgg-user-guides/SGG_25000/D1_AG_Validation_VEE_Rules.html) |
| Zero / minimum check | Count of zero intervals per day vs site history; kWh=0 while kvarh>0 = suspicious | AEMO §10.2(c): acceptable #zeros/day from historical data. UBP reactive check: fail if reactive >4 pulses when kWh=0 | AEMO; UBP §1.4.5 |
| Null / missing intervals, completeness | ≥1 non-null value per interval per datastream; "100% data set" | AEMO §10.2(d), §10.4(g) | AEMO |
| Pulse overflow, CRC, VT/phase failure, power-fail alarms | Validate data against meter alarms | AEMO §7.2 lists 5 mandatory alarms; ERCOT flags outages >3 s | AEMO; ERCOT SMOG §4.4 |
| High/Low usage vs history | Avg daily usage vs same month last year (or last month) | UBP: fail if deviation >25% of historical avg. Oracle High/Low Check normalises by Average Daily Usage | UBP §1.5.1; [Oracle High/Low](https://docs.oracle.com/en/industries/energy-water/meter-data-management/2.5.0.0.0/mdm-user-guides/MDM_25000/D1_AG_VEE_HighLowCheck.html) |
| Main/check meter comparison | Discrepancy between main and check meters | >1.5× class accuracy at full load → investigate. AEMO: nodal balance ≤1%/interval; remote check ≤5%/interval | BSCP502 App.4.1.7; AEMO §10.3 |
| Prolonged / excessive estimation | Share of estimated data | UBP guideline: ≤10% of data estimated per month; any meter estimated ≤3× in 12 months; Oracle "Prolonged Estimation Check" | UBP p.105; Oracle |
| DST / interval count | Quarter-hour days have 96 values; 92 on spring-forward day, 100 on fall-back day (German MaBiS) | BDEW MaBiS Q&A (search-only) | [BDEW MaBiS](https://www.bdew.de/media/documents/Awh_20150601_Umsetzungsfragenkatalog_MaBiS.pdf) |

**Estimation methods (precedence order, published):** UBP: gaps ≤2 h → linear
interpolation; >2 h → "same weekdays"/"like days" averages (up to 1 year history, exclude
power-failure and partial days, never estimate from estimates). AEMO type 17 linear
interpolation ≤2 h; type 14 like-day table; type 15 average like-day; type 19 zero; type 51
previous-year like day; type 21 divides 15/30-min history by 3/6 for 5-min conversion.
Elexon/MHHS: main→check meter copy → register-advance apportionment → average load shape
from same weekday over previous/following month → 2–3 weeks → 1 week → nearest 4 weeks →
profile-class defaults (default PF 0.9 for reactive). Germany: VDE-AR-N 4400 Metering Code
(Ersatzwertbildung rules; paywalled) with BSI TR-03109-1 SMGW status labelling; FfE
compares linear interpolation (≤2 h) vs comparison-day method
([FfE](https://www.ffe.de/veroeffentlichungen/ersatzwertbildung-fuer-energiewirtschaftliche-messungen-exemplarischer-vergleich-verschiedener-methoden/),
[VDE-AR-N 4400](https://www.vde.com/de/fnn/arbeitsgebiete/digitalisierung-metering/metering-code-vde-ar-n-4400),
[BSI TR-03109-1](https://www.bsi.bund.de/DE/Themen/Unternehmen-und-Organisationen/Standards-und-Zertifizierung/Smart-metering/Smart-Meter-Gateway/TechnRichtlinie/TR-03109-1.html)).
ERCOT and CAISO do **not** perform VEE themselves; TDSPs/Scheduling Coordinators must apply
UBP-style VEE before submitting settlement-quality data ([ERCOT SMOG §9](https://www.ercot.com/files/docs/2025/05/30/ERCOT-Settlement-Metering-Operating-Guide-June-1-2025.pdf);
[CAISO BPM Metering](https://bpmcm.caiso.com/BPM%20Document%20Library/Metering/BPM%20for%20Metering_v24_Redline.pdf), search-only).
EU: Implementing Regulation (EU) 2023/1162 sets the interoperability/role model for
validated metering data access, not numeric VEE rules
([EUR-Lex](https://eur-lex.europa.eu/eli/reg_impl/2023/1162/oj/eng)).

## 2. Solar PV plant monitoring

**Data shapes.** Irradiance (GHI/POA/DNI/DHI, W/m²), module/ambient temperature, wind,
DC/AC power per inverter/string, energy; IEC 61724-1:2021 Class A (utility) / Class B
(commercial) with Table 1 sampling and recording interval requirements, §12.2.2 treatment
of missing data, Annex A on sampling ([IEC 61724-1:2021](https://webstore.iec.ch/en/publication/65561);
[preview ToC](https://elstandard.se/documents/preview/2770101)). Typical recording 1–15 min;
PVDAQ public data is 15-min ([PVDAQ](https://data.openei.org/submissions/4568)).
IEC 61724-3:2016 specifies the basic filter set: logical thresholds, missing/duplicate,
stuck values and abrupt changes via derivatives
([Lindig et al. 2024](https://onlinelibrary.wiley.com/doi/full/10.1002/solr.202400634)).

| Check | Rule | Default threshold | Source |
|---|---|---|---|
| Physically possible irradiance limits (BSRN/QCRad) | GHI ≤ Sa·1.5·μ0^1.2+100; DHI ≤ Sa·0.95·μ0^1.2+50; DNI ≤ Sa; min −4 W/m² | "Extremely rare": GHI ≤ Sa·1.2·μ0^1.2+50, DHI ≤ Sa·0.75·μ0^1.2+30, DNI ≤ Sa·0.95·μ0^0.2+10, min −2 | [BSRN QC V2](https://bsrn.awi.de/fileadmin/user_upload/bsrn.awi.de/Publications/BSRN_recommended_QC_tests_V2.pdf); [pvanalytics irradiance.py](https://raw.githubusercontent.com/pvlib/pvanalytics/main/pvanalytics/quality/irradiance.py) |
| Component consistency | GHI/(DNI·cosZ+DHI) and DHI/GHI ratios | ±8% (SZA<75°), ±15% (75–93°); DHI/GHI <1.05 / <1.10; only when >50 W/m² | BSRN QC V2; pvanalytics `check_irradiance_consistency_qcrad` |
| Clear-sky exceedance | measured/clearsky | `clearsky_limits(csi_max=1.1)` | [pvanalytics API](https://pvanalytics.readthedocs.io/en/stable/api.html) |
| Daily insolation plausibility | daily measured/clearsky insolation | `daily_insolation_limits` 0.4–1.25 | pvanalytics |
| Stale / stuck values | Consecutive window all within tolerance | `stale_values_diff(window=6, rtol=1e-5, atol=1e-8)`; `stale_values_round(window=6, decimals=3)`; OpenOA `unresponsive_flag(threshold=3)` | [stale_values_diff](https://pvanalytics.readthedocs.io/en/stable/generated/pvanalytics.quality.gaps.stale_values_diff.html) |
| Interpolated (fake) data | Linear runs detected via 2nd difference | `interpolation_diff(window=6)` | pvanalytics gaps.py |
| Completeness | Fraction of expected samples per day | `complete(minimum_completeness=0.333)`; `trim(days=10)` | pvanalytics gaps.py |
| Inverter clipping | Flat top of daily 99.5th-percentile curve | `clipping.threshold(slope_max=0.0035, power_min=0.75, power_quantile=0.995)`; plus `levels`, `geometric` | [clipping.threshold](https://pvanalytics.readthedocs.io/en/stable/generated/pvanalytics.features.clipping.threshold.html) |
| Time-shift / DST / wrong timezone | Changepoint on solar-noon offset vs modelled | `shifts_ruptures(period_min=15, shift_min=15, zscore_cutoff=2)`; `has_dst(window=7, min_difference=45)`. NREL benchmark: PVAnalytics-CPD MAE 3.6 min (full DST), 1.8 min (wrong tz) | pvanalytics time.py; [NREL time-shift survey](https://docs.nlr.gov/docs/fy23osti/85699.pdf) |
| Outliers | Tukey / z-score / Hampel | `tukey(k=1.5)`, `zscore(zmax=1.5)`, `hampel(window=5, max_deviation=3.0)` | pvanalytics outliers.py |
| Data shifts (capacity change, sensor swap) | Changepoint on daily series | `detect_data_shifts`; solar-data-tools capacity-change detector | [detect_data_shifts](https://pvanalytics.readthedocs.io/en/stable/generated/pvanalytics.quality.data_shifts.detect_data_shifts.html); [solar-data-tools](https://github.com/slacgismo/solar-data-tools) |
| Pyranometer soiling/drift | Compare reference vs plant sensors / satellite | Pyranometer soiling biases module soiling estimate from PR by 30–43% (abstract-only) | [Solar Energy 2025](https://www.sciencedirect.com/science/article/abs/pii/S0038092X25003974) |
| Missing-data impact on PR | Gap sensitivity | 5 missing days can bias monthly PR by >5 %-points; 20 days: −10…+15% | Lindig et al. 2024 §4 |

## 3. Wind turbines / farms

**Data shapes.** 10-min mean/std/min/max per signal (wind speed, power, rotor/gen speed,
pitch, nacelle direction, temperatures), 80–950 columns per turbine (CARE dataset: farm A
86, B 257, C 957 features), plus status/event logs; IEC 61400-25 defines the information
model for SCADA exchange ([IEC 61400-25-1](https://webstore.iec.ch/en/publication/5438)).
IEC 61400-12-1 (2017/2022) requires 1 Hz sampling averaged to 10 min and contains clause
8.4 "Data rejection" and the method of bins ([IEC 61400-12-1:2017](https://webstore.iec.ch/en/publication/26603);
[ToC sample](https://cdn.standards.iteh.ai/samples/17046/768eb857b82a4b8d99aa87ef901d23ea/IEC-61400-12-1-2017.pdf)).
The exact rejection list is in the paywalled text (not verified here).

| Check | Rule | Default threshold | Source |
|---|---|---|---|
| Range flag | value outside [lower, upper] | OpenOA `range_flag` (user bounds) | [OpenOA utils](https://openoa.readthedocs.io/en/latest/api/utils.html) |
| Frozen sensor / stale counter | value unchanged N intervals | OpenOA `unresponsive_flag(threshold=3)` | OpenOA |
| Power-curve outliers | Bin by wind speed, flag > k·std from bin centre | OpenOA `bin_filter(threshold=2, center_type='mean', threshold_type='std')`; `std_range_flag(threshold=2.0)` | OpenOA |
| Multivariate cluster outliers | K-means + Mahalanobis | `cluster_mahalanobis_2d(n_clusters=13, dist_thresh=3.0)` | OpenOA |
| Curtailment / stoppage | Wind in operating range but power ≈0 → non-normal | CARE "enhanced filter"; status-ID 0 only is "normal" | [CARE to Compare](https://arxiv.org/html/2404.10320v2) |
| Duplicate / gap timestamps, DST | `duplicate_time_identification`, `gap_time_identification`, `daylight_savings_plot(hour_window=3)` | OpenOA qa | OpenOA |
| Implausible min/max/std statistics | Min>Mean, Std<0 etc. | CARE farm B: ~100% implausible for many signals → use averages only | CARE |
| Missing encoded as 0 | Zero-runs replacing NaN | CARE farms B/C | CARE |
| Icing | % deviation from OEM power curve / std or quantile per bin | method comparison (search-only) | [Pandit 2024 review](https://ietresearch.onlinelibrary.wiley.com/doi/10.1049/rpg2.12920) |

Open datasets: Kelmarsh (6× MM92, 2016–2024) and Penmanshiel (14× MM82, 2016–2021), 10-min
SCADA+events, CC-BY-4.0 ([Penmanshiel Zenodo](https://zenodo.org/records/8253010)); EDP
(4 turbines, 2016–17, 83 SCADA columns, failure log) ([EDP](https://www.edp.com/en/innovation/data),
[Mendeley](https://data.mendeley.com/datasets/zjxjnjp3xs/1)); CARE (36 turbines, 89
turbine-years, [Zenodo](https://zenodo.org/records/14006163)); catalogue at
[OpenWindSCADA](https://github.com/sltzgs/OpenWindSCADA) (also Hill of Towie 21 turbines/655
variables, Anholt, Dundalk). Quality-issue taxonomy: [Leahy et al. 2019](https://doi.org/10.3390/en12020201);
power-curve cleaning benchmark [Renewable Energy 2021](https://www.sciencedirect.com/science/article/abs/pii/S0960148121017134).

## 4. Grid: PMU, SCADA/state estimation, ENTSO-E, CIM/CGMES, DSO LV

**PMU/synchrophasor.** 30–60 frames/s; IEEE C37.118.2 16-bit STAT word: bit15 data valid,
bit14 PMU error, bit13 sync, bit12 sort-by-arrival, bit10 config change, bits 5-4 unlocked
time ([status-word paper](https://www.osti.gov/servlets/purl/1891313);
[C37.118.2](https://standards.ieee.org/ieee/C37.118.2/7077/)). NASPI PARTF framework
defines completeness attributes: gap rate, mean gap size, largest gap (per PMU and per
aggregator), plus temporal/geospatial/topological accuracy, TVE, latency, and lineage; cites
the 2015 leap-second duplicate-timestamp incident
([NASPI PARTF whitepaper](https://www.naspi.org/sites/default/files/reference_documents/PARTF_WhitePaper_20170314_Final_PNNL_26313.pdf)).
Field statistics (79 PMUs, 2016–17): most PMUs missing 5–20% of key measurements, some
100%; 18 PMUs with flat 60.00 Hz frequency = config error. DataNXT/WECC validation set: range
check, stale check, noise check, time-error/time-quality validation, status-flag analysis
([DataNXT](https://www.naspi.org/sites/default/files/2017-05/martin_chen_dataquality_20160321.pdf)).

**SCADA state estimation.** Chi-square test on J(x) for detection; Largest Normalized
Residual for identification; the conventional LNR threshold of 3.0 is textbook (Abur &
Expósito), not verified from a URL here ([ScienceDirect topic](https://www.sciencedirect.com/topics/engineering/bad-data-detection);
[arXiv 2001.10764](https://arxiv.org/pdf/2001.10764)).

**ENTSO-E Transparency.** Hirth et al. 2018: Actual Total Load gaps 2015–16 e.g. Cyprus 86%,
Malta 100%, Sweden 7.5%; load deviations vs national sources often >10%; DST "a notorious
weak spot"; only 55% of borders without gaps
([paper](https://www.sciencedirect.com/science/article/pii/S0306261918306068)). ACER 2024/25
urges ENTSO-E to improve balancing data quality
([ACER](https://www.acer.europa.eu/news/acer-urges-entso-e-improve-balancing-data-quality-and-adjust-reporting-schedule)).

**CIM/CGMES.** ENTSO-E QoCDC (v4.1.4, Nov 2025) defines 8 validation levels (file metadata,
syntax, constraints, assembly, cross-profile consistency, IGM/CGM plausibility,
coordination, convergence), severities ERROR/WARNING; numeric examples: SSH_SV_MAX_P_DIFF
10 MW, SSH_SV_TOT_P_DIFF 200 MW, INTERCH_IMBALANCE_WARNING 50 MW / ERROR 200 MW, power-flow
mismatch 0.1 MW/MVAr per node ([QoCDC v3.2.1](https://eepublicdownloads.azureedge.net/clean-documents/digital/QualityOfCGMESdatasetsAndCalculations_v3_2_1.pdf);
[library](https://www.entsoe.eu/data/cim/cim-for-grid-models-exchange/)). SHACL
implementations: [cgmes-modeling-shacl](https://github.com/cimcgmes/cgmes-modeling-shacl),
[ModShape](https://github.com/griddigit-ci/ModShape).

**DSO LV.** Smart-meter voltage data used for phase identification (>99% label correction
reported) — implies checks for mislabeled phase/topology and timestamp alignment
([EPSR 2022](https://www.sciencedirect.com/science/article/abs/pii/S0378779622006253), search-only).

## 5. Gas, district heating, hydro, BESS, energy balance

| Domain | Check | Rule / threshold | Source |
|---|---|---|---|
| Energy balance (DSO/TSO) | Σ feeder/customer energy vs substation input; losses in expected band | CEER 2022 data: distribution losses 1.95–22.63%, transmission 0.99–3.96% (set per-network band) | [CEER 3rd Power Losses report](https://www.ceer.eu/publication/3rd-ceer-report-on-power-losses/) |
| Metering nodal balance | Sum of flows to/from busbar | AEMO: ≤1% per interval | AEMO §10.3 |
| Gas | Volume ≥0; same MPRN from multiple providers with different volumes → reject; standard conversion factor 1.02264 | Xoserve | [Xoserve](https://www.xoserve.com/media/3093/request-xxx-use-of-a-standard-national-conversion-factor.pdf) (search-only) |
| District heating | EN 1434-6 operational monitoring; paired temperature sensors; DQ dimensions completeness/typicality/consistency/uniqueness/timeliness with Mahalanobis typicality | Hourly data, ~7,000 meters, modified z-scores (Danish study) | [EN 1434-6](https://standards.globalspec.com/std/1023637/ds-en-1434-6); [Appl. Sci. 2026](https://www.mdpi.com/2076-3417/16/15/7478); [arXiv 2510.00872](https://arxiv.org/abs/2510.00872) |
| BESS | Cell-voltage spread, temperature spread, SOH; SOC drift from current-sensor offset (Ah-integration) | No fixed thresholds published | [arXiv 2601.03007](https://arxiv.org/html/2601.03007); [ITEES 2026](https://onlinelibrary.wiley.com/doi/10.1155/etep/4524955) |
| Hydro | Density-clustering anomaly cleaning; efficiency vs head/discharge plausibility | — | [PMC10781255](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC10781255/) (search-only) |

## 6. Energy trading / market data

- SDAC day-ahead switched to 15-min MTU on 30 Sep 2025 (delivery 1 Oct 2025): 96
  quarter-hours/day; Ireland stays 30-min ([pv-magazine](https://www.pv-magazine.com/2025/10/01/european-electricity-spot-market-shifts-to-15-minute-trading-blocks/);
  [EPEX](https://www.epexspot.com/en/news/15-minute-products-live-epex-spot-day-ahead-markets)).
  **Checks:** expected interval count per delivery day = 96, except 92 (March) / 100
  (October) in local time; store in UTC with a local-time column (OPSD provides
  `utc_timestamp` + `cet_cest_timestamp`).
- OPSD: gaps ≤2 h linearly interpolated and flagged in a marker column; longer gaps left as
  NaN; DST "a source of confusion" ([OPSD paper](https://arxiv.org/pdf/1812.10405);
  [package](https://data.open-power-system-data.org/time_series/)).
- Forecast-vs-actual: Elia publishes quarter-hourly wind/PV measured + day-ahead/intraday
  forecasts ([ods086](https://opendata.elia.be/explore/dataset/ods086/table/),
  [ods032](https://opendata.elia.be/explore/dataset/ods032/)); check missing day-ahead
  intervals, forecast-horizon monotonicity, and actual within [0, installed capacity].

## 7. Datasets, reference implementations, literature

**Open-source implementations:** [PVAnalytics](https://github.com/pvlib/pvanalytics)
(quality.irradiance/gaps/outliers/time/weather/data_shifts; features.clipping);
[solar-data-tools](https://github.com/slacgismo/solar-data-tools);
[OpenOA](https://github.com/NREL/OpenOA) (utils.qa, utils.filters);
[BSRN Toolbox](https://wiki.pangaea.de/wiki/BSRN_Toolbox);
[cgmes-modeling-shacl](https://github.com/cimcgmes/cgmes-modeling-shacl),
[ModShape](https://github.com/griddigit-ci/ModShape);
[OPSD processing notebooks](https://github.com/Open-Power-System-Data/time_series/blob/master/processing.ipynb).

**Datasets:** PVDAQ ([OEDI](https://data.openei.org/submissions/4568)); NREL labelled
time-series data shifts ([OSTI](https://www.osti.gov/dataexplorer/biblio/dataset/1874783));
Kelmarsh/Penmanshiel, EDP, CARE, Hill of Towie; OPSD; Elia; ENTSO-E TP; NREL PERFORM
forecast+actuals ([OSTI](https://www.osti.gov/biblio/2335360)); campus smart-meter database
with 0–20% missing rates ([Sci. Data 2024](https://www.nature.com/articles/s41597-024-04106-1));
open PV reliability datasets review ([Appl. Energy 2025](https://www.sciencedirect.com/science/article/pii/S0306261925008621)).

**Papers 2020–2026:** smart-meter anomaly taxonomy (sudden jumps, stagnation, reverse
readings, pulse spikes, gradual drift, replacement jumps, retransmission duplicates,
timestamp errors) ([Sensors 2026](https://doi.org/10.3390/s26165122)); smart-meter
imputation benchmark incl. TS foundation models ([arXiv 2501.07276](https://arxiv.org/html/2501.07276v1));
PV KPI/data-quality review ([Lindig 2024](https://onlinelibrary.wiley.com/doi/full/10.1002/solr.202400634));
PV shift detection ([Perry & Muller 2022](https://ieeexplore.ieee.org/document/9938675/));
wind SCADA DQ ([Leahy 2019](https://doi.org/10.3390/en12020201),
[Pandit 2024](https://ietresearch.onlinelibrary.wiley.com/doi/10.1049/rpg2.12920)); PMU
status word ([OSTI 1891313](https://www.osti.gov/servlets/purl/1891313)).

## Standards — what each says about data quality

| Standard | DQ content |
|---|---|
| NAESB UBP Unbundled Metering | Full VEE algorithm set with numbers (time 3 min/55 min, sum ≤2 multipliers, spike 1.8 & 10 pulses, reactive 4 pulses, high/low 25%, interpolation ≤2 h, ≤10% estimated/month) |
| AEMO Metrology Procedure Part B (v7.02+) | Mandatory validations (max/spike, min/zero, null, alarms, continuity), check-meter tolerances 1%/5%, substitution types incl. 17 linear ≤2 h, 21 five-minute conversion |
| Elexon BSCP502 v36+ / MHHS METH002 | Outstation time 20 s/15 min; mini-MAR ±0.7% weekly/±5% daily; MAR ±0.1%; max kWh table with 20% rule; main/check 1.5× class accuracy; 8-tier estimation precedence |
| VDE-AR-N 4400 / BSI TR-03109-1 | Status labelling of values from SMGW, Ersatzwert rules (paywalled/spec) |
| IEC 61724-1:2021, -3:2016 | Class A/B, sampling/recording intervals (Table 1), missing-data treatment (§12.2.2), basic filters (stuck, abrupt change, duplicates, thresholds) |
| BSRN QC V2 (Long & Dutton) | Physically-possible/extremely-rare limits, component-sum and diffuse-ratio tests |
| IEC 61400-12-1 / -25 | 10-min averaging, data rejection clause, bins; SCADA information model |
| IEEE C37.118.1/.2, NASPI PARTF | TVE, STAT-word flags, completeness (gap rate/size), latency, timestamp accuracy |
| ENTSO-E QoCDC v4.1.4 | 8 validation levels, ERROR/WARNING, MW mismatch thresholds |
| EN 1434-6 | Heat-meter installation/operational monitoring |
| EU 2023/1162 | Metering-data access/interoperability reference model (no numeric VEE) |

## Top 15 energy-specific checks (ranked by cross-domain applicability × regulatory weight)

1. **Interval sum vs register advance (with rollover)** — UBP ≤2 multipliers; Elexon ±0.7%/±5%; MHHS ±0.1%.
2. **Expected interval count per day incl. DST (96/92/100; 48/46/50)** and clock/time-tolerance (UBP 3 min; Elexon 15 min).
3. **Null/missing intervals & completeness score** (AEMO "100% data set"; pvanalytics `complete` 0.333; NASPI gap rate/mean/largest gap).
4. **Spike check** highest vs 3rd-highest per 24 h >1.8 (UBP/Oracle).
5. **Capacity/maximum plausibility** (CT-ratio or CoP max kWh; PV ≤ installed capacity; irradiance BSRN limits).
6. **Stale/frozen values** (pvanalytics window 6; OpenOA 3 intervals; PMU flat 60 Hz).
7. **Negative / zero-run checks** (import ≥0; #zeros/day vs history; kWh=0 with kvarh>0).
8. **Main/check or redundant-source comparison** (1.5× class accuracy; AEMO 1%/5%; SCADA vs meter).
9. **Energy/nodal balance & loss band** (feeders vs substation; CEER loss ranges; CGMES interchange 50/200 MW).
10. **Time-shift/timezone/DST detection via solar-noon changepoint** (PVAnalytics CPD, MAE ≈2–5 min).
11. **Clipping/curtailment flagging** (PV `clipping.threshold`; wind "wind in range, power≈0").
12. **Physical consistency ratios** (GHI vs component sum ±8/15%; DHI/GHI <1.05; heat supply>return).
13. **Power-curve/bin outlier** (OpenOA bin_filter 2σ, Mahalanobis 3.0).
14. **Estimated-data share & prolonged estimation** (≤10%/month; ≤3 per meter/year).
15. **Status/quality-flag propagation** (PMU STAT bits; meter alarms; SMGW status; A/E flags) — never drop flags on aggregation.

*Gaps to note:* numeric IEC 61724-1 Table 1 values and IEC 61400-12-1 clause 8.4 rejection
list are behind paywalls (only ToC verified); Oracle/Itron/L+G vendor defaults are
configurable and not published; BESS has no published DQ thresholds beyond study-specific
values.
