# Research 05 — Time-series data quality in oil and gas

*Research date: 2026-09-21. Deep-read: the 3W Dataset 2.0.0 paper, the Norwegian Offshore
Directorate's guidelines to the measurement regulations (PDF, text-extracted), AVEVA's
exception/compression presentation (PDF), CMR's multiphase-meter uncertainty paper (PDF) and
the open62541 status-code table. Items marked (search-only) come from search snippets and
were not deep-read. Extends `02-energy-domain-quality.md`; the generic checks in
`docs/checks/catalogue.md` apply unchanged, this note records what is specific.*

Oil and gas is the home turf of process historians. The same 20 generic checks run on
pressures, temperatures, flows, levels, valve states and choke positions; what changes is the
mix of faults (frozen tags and compression artefacts dominate), the metadata needed to avoid
false positives (shut-in state, valve line-ups, historian compression settings) and the
regulatory numbers that turn a statistic into a finding (fiscal uncertainty limits, leak
detection thresholds, allocation tolerances).

## 1. Wells and subsea production (the 3W corpus)

**Data shapes.** Per well: downhole gauge pressure and temperature (PDG), wet-christmas-tree
transducer (TPT), pressures and temperatures upstream and downstream of the production choke
(P-MON-CKP, P-JUS-CKP, T-JUS-CKP), gas-lift choke pressures and rate (P-MON-CKGL, P-JUS-CKGL,
QGL), annulus pressure, choke openings in %, and valve states (DHSV, master, wing, crossover
and shutdown valves, encoded 0 / 0.5 / 1). Petrobras' 3W Dataset 2.0.0 publishes exactly this
shape: 27 variables at 1 Hz, 2,228 instances (1,119 real from 42 wells between mid-2011 and
mid-2023, 1,089 simulated, 20 hand-drawn), labelled with nine undesirable event types
(abrupt BSW increase, spurious DHSV closure, severe slugging, flow instability, rapid
productivity loss, quick restriction in the production choke, scaling in the choke, hydrate in
the production line, hydrate in the service line) plus a transient label offset of 100
([3W 2.0.0 paper](https://arxiv.org/html/2507.01048v1); [Figshare, CC BY 4.0](https://doi.org/10.6084/m9.figshare.29205836.v1);
[GitHub project](https://github.com/petrobras/3W)).

**Data-quality facts stated by the authors.** Real instances are "intentionally left
untreated": 65.9 % of variable slots are missing entirely (41,109 of 62,384), 9.77 % are
*frozen* for the whole instance (6,095), and 5.26 % of observations are unlabelled. Simulated
and hand-drawn instances have no frozen or missing variables. Missing variables appear as
all-null columns; frozen variables as one repeated value.

| Check | Rule | Default / source |
|---|---|---|
| Frozen tag | A measurement that holds one value for an entire window is a stuck sensor or a dead historian link, even if the baseline profile (possibly computed on the same period) says "constant" | `tby.flatline` frozen rule: ≥ 99 % of usable samples equal the first, once the frame has at least `min_run` (default 10) usable samples; found on 3W real instances (see §9) |
| Shut-in awareness | Flat, zero or floor values while the well is shut in are not faults. Use DHSV/master/wing valve states (0 = closed) and choke opening 0 % to mask flatline, non-negative and range findings | 3W valve-state variables; a `well_state` companion series |
| Pressure/temperature consistency | Downhole P-PDG and T-PDG move together; upstream choke pressure ≥ downstream; a step in one without the other is instrumentation | `tby.correlation_break` (sprint 4) on (P-PDG, T-PDG), (P-MON-CKP, P-JUS-CKP) |
| Choke vs flow | Choke opening > 0 with zero gas-lift flow (QGL) for hours, or flow with choke 0 %, is a metering or tag-mapping fault | cross-series rule; needs QGL and ABER-CKGL |
| Event-shaped signatures | The nine 3W events are not data faults; Tabayyun should *not* flag them as quality issues but can expose them as "operational anomaly, confirm" using the same changepoint machinery | keep `tby.changepoint` findings at medium and label "confirm as legitimate or fault" |

## 2. Historian exception and compression (PI and equivalents)

AVEVA's own guidance (deep-read) gives the defaults and their consequences
([UC23 presentation](https://cdn.osisoft.com/osi/presentations/2023-AVEVA-San-Francisco/UC23NA-3PGK04-AVEVA_Bregenzer_Brent-Exception-Compression-and-their-Impacts-On-PI-System-Performance.pdf)):

| Attribute | Default | Meaning |
|---|---|---|
| `ExcDevPercent` / `ExcDev` | 0.1 % of span / 0.1 | Interface dead band: a value within ExcDev of the last *sent* value is dropped at the interface |
| `ExcMax` | 600 s | A value is sent at least every 10 min even if inside the dead band |
| `CompDevPercent` / `CompDev` | 0.2 % of span / 0.2 | Swinging-door deviation: archived points are those needed to reconstruct the signal within CompDev by linear interpolation |
| `CompMax` | 28,800 s (8 h) | A point is archived at least every 8 h |
| `Span` | 100 | Span the percentages refer to; percent values override engineering-unit values |
| Recommended | CompDev ≤ instrument precision; ExcDev = ½ CompDev; non-critical analogs CompDevPercent 0.1–2 | |

Consequences for quality checks: (a) retrieval with interpolation makes segments between
archived points perfectly linear, so linear runs are the *expected* signature of a compressed
tag, not of an interpolation fault; (b) a signal quieter than CompDev archives nothing for up
to 8 h, which looks identical to a frozen sensor; (c) resolution appears to degrade when the
signal noise drops below CompDev. Tabayyun should ingest the tag's ExcDev/CompDev/CompMax
where the connector exposes them and: treat linear runs whose endpoints are archived points as
compression (informational), report a flat run as frozen only if archived points exist inside
it or the run exceeds CompMax, and express `resolution_loss` relative to CompDev. Search-only
sources agree on the defaults ([PI Square](https://community.aveva.com/pi-square-community/f/forum/89547/compdev-and-other-attributes-standard);
[PiSharp](https://www.pisharp.com/article/359/understanding-exception-and-compression-in-pi-data-archive)).

**OPC UA / OPC DA quality.** OPC UA Part 8 DataAccess status codes carry the quality that
Tabayyun normalises to good/uncertain/bad. Relevant codes (open62541 table, deep-read
[status codes](https://open62541.org/doc/1.0/statuscodes.html)): Uncertain_LastUsableValue
0x40900000, Uncertain_NoCommunicationLastUsableValue 0x408F0000, Uncertain_InitialValue
0x40920000, Uncertain_SensorNotAccurate 0x40930000, Uncertain_EngineeringUnitsExceeded
0x40940000, Uncertain_SubNormal 0x40950000; Bad_NoCommunication 0x80310000,
Bad_WaitingForInitialData 0x80320000, Bad_ConfigurationError 0x80890000, Bad_NotConnected
0x808A0000, Bad_DeviceFailure 0x808B0000, Bad_SensorFailure 0x808C0000, Bad_OutOfService
0x808D0000, Bad_DataLost 0x809D0000, Bad_DataUnavailable 0x809E0000; Good_LocalOverride
0x00960000 and Good_Clamped 0x00300000 are "good" values that a quality check must still
treat as suspicious (manual override, clamped at a limit). OPC DA packs quality into
`QQSSSSLL` bits (quality, sub-status, limit) and the COM UA proxy maps between the two
([OPC UA Part 8 A.4.3](https://reference.opcfoundation.org/v104/Core/docs/Part8/A.4.3/), search-only).
Recommendation: keep the raw code in evidence, map `Good_LocalOverride`, `Good_Clamped` and
all `Uncertain_*` to *uncertain*, and add a `quality_flags` sub-finding "manual override" and
"clamped at limit" because both hide sensor problems behind a good flag.

## 3. Fiscal and allocation metering

**Norway (deep-read guidelines).** The measurement regulations fix uncertainty limits per
measurand in Section 10 Table 1; the guidelines' worked examples use a relative expanded
uncertainty (95 %) of **0.30 %** for net standard volume of oil delivered from a field or a
pipeline outlet and 0.25 % for another delivery system, note that a systematic error above
0.02 % of the quantity must be corrected, and allow licensees to define other limits for
*allocation* measurement when meeting Table 1 is technically infeasible or unreasonably costly,
which "mainly applies" to multiphase measurement and single-stage separator outlets
([Guidelines to the measurement regulations](https://www.sodir.no/49122d/globalassets/1-sodir/regelverk/veiledninger/guidelines-for-regulations-relating-to-fiscal-measurement-in-the-petroleum-activities.pdf),
Re Section 10). The first validation of an allocation system is due within one year of
start-up (Re Section 20). The regulation text itself was not readable through the proxy
([regulations page](https://www.sodir.no/en/regulations/regulations/regulations-relating-to-fiscal-measurement-in-the-petroleum-activities/),
search-only); the gas limit commonly quoted as 1.0 % is therefore unconfirmed here.

**Multiphase flow meters used for allocation (deep-read).** A typical MPFM specification at
95 % confidence: liquid volume flow 2.5 % for GVF < 80 % and 5 % for GVF > 80 %; gas volume
flow 5 %; water-liquid ratio 2 % absolute for WLR < 85 % and 1 % absolute above; oil and gas
densities from PVT 3 %. Corrected hydrocarbon mass approaches the test-separator uncertainty
only when K-factors from separator campaigns are applied and WLR/GVF stay in the calibrated
range ([CMR, NFOGM 2013](https://nfogm.no/wp-content/uploads/2019/02/2013-24-Uncertainty-analysis-of-multiphase-flow-meters-used-for-allocation-measurements-Folger%C3%B8-CMR.pdf)).
The NFOGM MPFM handbook is the reference text ([Handbook rev. 2](https://nfogm.no/wp-content/uploads/2014/02/MPFM_Handbook_Revision2_2005_ISBN-82-91341-89-3.pdf), search-only).

**Allocation and custody transfer (search-only).** Industry allocation tolerance is quoted at
5–10 %, with observed 12–15 % deviations between actual and theoretical results flagged as
out of tolerance; MPFM totals versus sales-meter totals carry contractual tolerances of
1–2 % ([review of allocation methods](https://www.researchgate.net/publication/336725288_A_review_of_hydrocarbon_allocation_methods_in_the_upstream_oil_and_gas_industry);
[Trilogy](https://trilogyes.com/blog/oil-and-gas-production-allocation-software-preventing-imbalances-and-revenue-leakage)).
API MPMS Chapter 4 proving: five consecutive prover runs within 0.05 % repeatability, meter
factor uncertainty ±0.027 % at 95 % ([MPMS 4.8](https://standards.globalspec.com/std/14398357/mpms-4-8)).
Custody transfer metering-station admissible uncertainty 0.3 %, LPG 1.0 %
([ScienceDirect 2025](https://www.sciencedirect.com/science/article/abs/pii/S0009250925010693)).

| Check | Rule | Default / source |
|---|---|---|
| Meter-factor drift | Meter factor from successive provings trends or jumps; repeatability of the five runs > 0.05 % | API MPMS Ch. 4 (search-only) |
| Allocation imbalance | Σ well allocations (MPFM or well-test based) vs fiscal export over the allocation period | contractual 1–2 % (MPFM vs sales), 5–10 % industry (search-only); Norway Table 1 per measurand |
| Well test vs MPFM | Test-separator rate vs MPFM rate outside the MPFM's stated uncertainty (2.5–5 % liquid, 5 % gas, 1–2 % abs WLR) | CMR/NFOGM specification |
| GVF/WLR range | MPFM operating outside its calibrated GVF/WLR envelope → rates are estimates, mark quality *uncertain* | CMR paper §example 3 |
| Systematic error | Persistent bias > 0.02 % of quantity on fiscal meters must be corrected | Norwegian guidelines Re Section 10 |

## 4. Pipelines: SCADA and computational pipeline monitoring

49 CFR 195.444 requires every CPM leak-detection system on a single-phase hazardous liquid
pipeline to comply with API RP 1130, incorporated by reference, and 195.134 requires
operators to evaluate the capability of their leak detection (compliance for all single-phase
liquid lines by 1 October 2024)
([eCFR 195.444](https://www.ecfr.gov/current/title-49/subtitle-B/chapter-I/subchapter-D/part-195/subpart-F/section-195.444);
[eCFR 195.134](https://www.ecfr.gov/current/title-49/subtitle-B/chapter-I/subchapter-D/part-195/subpart-C/section-195.134)).
API RP 1130 describes CPM as algorithmic monitoring of hydraulic state measurements (flow,
pressure, temperature, density, viscosity, valve and pump status) with mass, momentum and
energy conservation models; volume-balance methods compare inbound and outbound flow, and the
threshold trades small-leak sensitivity against false alarms from transients
([API RP 1130](https://www.api.org/products-and-services/standards/important-standards-announcements/rp1130);
[Wikipedia overview](https://en.wikipedia.org/wiki/Leak_detection); both search-only). Flow
meter accuracy for CPM is quoted at ±0.1 % to ±0.5 % of rate (search-only,
[IMD guide](https://industrialmonitordirect.com/blogs/knowledgebase/pipeline-leak-detection-threshold-values-api-11301149-compliance-guide)).

Quality implications: CPM is only as good as its inputs, so the classic data checks map
directly. Inlet and outlet meters must be time-aligned (timestamp skew between RTUs shows up
as a phantom imbalance during transients), comm loss must propagate as *bad* quality rather
than a repeated last value, and a frozen or drifting flow meter produces a slowly growing
balance residual. `tby.balance_residual` (sprint 4) with the CPM tolerance as threshold and
`tby.latency`/`tby.staleness` on each RTU feed cover this.

## 5. Process plants: data validation and reconciliation (VDI 2048)

VDI 2048 defines process-data reconciliation: constraints from closed mass, material and
energy balances, measurement uncertainties and correlations, a check whether the measured
values satisfy the constraints, gross-error detection at 95 % confidence and an F-test before
reconciled values and their reduced uncertainties are computed
([Ebsilon VDI 2048 help](https://help.ebsilon.com/EN/VDI2048_Validierung.html);
[IAEA INIS record](https://inis.iaea.org/records/dj775-4eg92); [KNS paper](https://www.kns.org/files/pre_paper/31/675%EC%9D%B4%ED%95%9C%EC%84%A4.pdf);
all search-only). This is the formal backing for `tby.balance_residual` and the planned
`reconcile.balance` repair operation: a residual outside the propagated uncertainty of the
balance is a gross error in one of the members, and the member with the largest standardised
correction is the suspect.

## 6. Safety systems and redundant transmitters

Safety instrumented systems use 1oo2 or 2oo3 voting on redundant transmitters; 2oo3 masks a
single failure without spurious trips and requires at least two of three channels to agree
([Automation Forum](https://automationforum.co/redundant-transmitters-voting-logic-sil/);
[Siemens F-AI voting note](https://cache.industry.siemens.com/dl/files/377/24690377/att_890768/v2/24690377_Wiring_Voting_AI_V30_en.pdf);
search-only). Discrepancy analysis between channel pairs is standard, but no public source
found here states a numeric deviation threshold; vendors configure it per loop (typically a
few percent of span, unsourced practice). Tabayyun's `tby.redundant_disagreement` (sprint 4)
should default to a *learned* threshold: the historical spread between the redundant channels
plus a margin, with the SIS discrepancy setting as an override when known. A channel whose
deviation grows steadily is the classic pre-failure signature the SIS itself will not report
until the trip limit.

## 7. Alarms and events

EEMUA 191 and ISA-18.2 set an average of about one alarm per 10 minutes per operator in
steady state and define an alarm flood as more than 10 alarms in 10 minutes; both track stale,
chattering and fleeting alarms as KPIs ([Merobix comparison](https://www.merobix.com/blog/isa-18-2-vs-eemua-191);
[Industry Digits scorer](https://industrydigits.com/resources/alarm-load-scorer/); [exida](https://www.exida.com/Alarm-Management/Detail/standards_guidelines);
search-only). Alarm and event logs are point series in Tabayyun's model; the checks are
count-rate (flood), chattering (repeat interval below a limit) and stale (active longer than a
limit), and alarm bursts are strong context for explaining spikes and changepoints in the
process tags.

## 8. Drilling (WITSML)

Real-time drilling data is depth- and time-indexed (WITSML, Energistics), and the literature
reports missing channels and data fragments, connectivity loss, hole-depth resets during
re-logging or tripping, and outliers that engineers remove by hand before use
([SPE-181038-MS](https://onepetro.org/SPEIE/proceedings-abstract/16IE/16IE/SPE-181038-MS/186740);
[SPE GOTS 2025](https://onepetro.org/SPEGOTS/proceedings-abstract/25GOTS/25GOTS/652851);
[Energistics WITSML](https://energistics.org/witsml-data-standards); search-only). Depth-indexed
logs need a depth-monotonicity check (the analogue of `timestamp_integrity`), and rig-state
segmentation (drilling, tripping, circulating) plays the same masking role as shut-in state
for wells. Out of scope for R1; noted for the roadmap.

## 9. Measured behaviour of the current checks on 3W data

Six real 3W instances were pulled from the Figshare archive (classes 0, 1, 2, 5, 7 and 8) and
eight tags converted to `ts,value` CSV. Findings with `tabayyun run --profile` before and after
the fixes made in this sprint:

| Series | Rows | Before | After / expected |
|---|---|---|---|
| class 0 normal, P-PDG (frozen for the whole instance) | 21,474 | score 100, no finding: the profile computed on the frozen period said "constant" and the check skipped | one `flatline` finding "Series frozen at … for the whole window" |
| class 0 normal, P-TPT | 21,474 | 43 `interpolation_artifacts`, linear share 48 % | share unchanged; the runs are compression signatures (§2) |
| class 0 normal, T-JUS-CKP | 21,474 | 200 findings, linear share reported as **102 %** | share ≤ 100 % (overlapping runs fixed) |
| class 2 spurious DHSV closure, P-TPT | 12,721 | 1 `flatline` (1 h 30 m stuck at 18.85 MPa) plus 76 interpolation findings | the stuck run is the event itself: shut-in awareness would downgrade it |
| class 5 rapid productivity loss, P-PDG | 24,868 | 200 interpolation findings, share 33 % | |
| class 7 scaling in choke, P-MON-CKP | 76,633 | 200 interpolation findings, share 102 % | share ≤ 100 % |
| class 8 hydrate in production line, P-PDG | 314,604 | `resolution_loss` step 1 → 10 Pa, 4 `distribution_drift`, 200 interpolation | resolution loss is real (gauge quantisation changed) |

Two conclusions. First, frozen tags are the dominant real fault and the check now reports
them regardless of the profile. Second, linear runs are the normal texture of compressed
historian data at 1 Hz; per-run findings must become one finding per window with the share
and the longest run as evidence, and the compression settings (§2) should decide whether the
run is a fault at all. That aggregation is tracked with the energy false-positive work.

## 10. Datasets for testing

| Dataset | Content | Access |
|---|---|---|
| Petrobras 3W 2.0.0 | 2,228 well instances, 27 tags at 1 Hz, nine labelled events, real frozen/missing variables | [Figshare](https://doi.org/10.6084/m9.figshare.29205836.v1), one 1.8 GB zip, CC BY 4.0; members can be read individually with HTTP range requests |
| Equinor Volve | Daily production per well 2008–2016, real-time drilling data, reservoir models | [Databricks Marketplace](https://www.equinor.com/energy/volve-data-sharing) under the Equinor Open Data Licence; no direct file download (deep-read page) |
| SKAB | Valve/pump testbed with labelled anomalies | GitHub `waico/SKAB` (search-only) |
| Tennessee Eastman | Simulated chemical process with 21 fault types | many mirrors (search-only) |

## Top 12 oil-and-gas-specific checks (ranked by frequency × damage × implementability)

1. **Frozen tag** — implemented this sprint as the `flatline` frozen rule.
2. **Shut-in / valve-state masking** — companion state series suppresses flatline, non-negative
   and range findings while the well is closed in.
3. **Compression-aware linear runs** — one finding per window; PI ExcDev/CompDev/CompMax from
   the connector decide fault vs expected texture.
4. **Redundant transmitter disagreement** — learned spread with SIS override (sprint 4).
5. **Balance residual with propagated uncertainty** (VDI 2048 style) — pipeline CPM, separator
   and plant balances (sprint 4).
6. **Timestamp skew across RTUs** — inlet/outlet alignment before balance checks.
7. **Quality-code semantics** — `Good_LocalOverride`, `Good_Clamped` and `Uncertain_*` mapped
   to uncertain with explicit sub-findings.
8. **Allocation imbalance** — Σ allocations vs fiscal export against contractual tolerance.
9. **Well test vs MPFM** — outside the meter's stated uncertainty, or outside its GVF/WLR
   envelope.
10. **Meter-factor drift** — proving history as a series; repeatability > 0.05 %.
11. **Choke vs flow consistency** — opening vs gas-lift / production flow.
12. **Alarm rate, chattering, stale** — EEMUA 191 / ISA-18.2 KPIs on event series.
