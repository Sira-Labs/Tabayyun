# Tabayyun — Product Vision

> *Tabayyun* (تبيّن): to verify, to make clear, to ascertain the truth of a report before acting on it.

Tabayyun is a self-hostable platform that continuously **verifies the trustworthiness of
industrial time series** before people, models and control decisions rely on them. It
profiles every tag, runs a curated library of statistical and domain checks, scores the
result, explains *why* a series is untrustworthy, and routes findings to the people who
can fix them.

Initial target vertical: **energy** (solar PV, wind, grid/SCADA, smart metering, storage),
because the domain has published validation standards, large open datasets, and a
recurring pain: forecasts, settlements and asset-performance analytics built on data that
was silently wrong.

## Why now

- Historians (PI, OPC UA servers, SCADA, MDM) hold decades of data whose quality nobody
  measures. Data teams discover problems downstream, in models and reports.
- Generic data-quality tools (Great Expectations, Soda, dbt tests) are table/batch-shaped.
  They do not understand sampling rate, staleness, compression artefacts, drift, or
  correlation between physically related signals.
- Foundation models and forecasting are only as good as the input. Quality *scores per
  series* become a first-class input to every model pipeline.

## Product principles

1. **Explain, don't just flag.** Every finding carries evidence: the window, the statistic,
   the threshold, the comparison baseline, and a plain-language explanation.
2. **Opinionated defaults, transparent thresholds.** Every check ships with a default that
   works on real energy data and is visible and editable by the user.
3. **Series first, table second.** The unit of work is a time series with metadata
   (unit, sampling interval, physical limits, asset relations), not a table row.
4. **Local by default.** One binary or one `docker compose up`; air-gapped friendly.
   No phone-home for core functionality.
5. **Secure by design.** OIDC login (Google, Entra, Keycloak), RBAC at organisation,
   workspace and resource level, audit log, no secrets in code or config files.
6. **Extensible.** Custom checks in Python (and later a declarative DSL) with the same
   evidence and scoring model as built-in checks.

## Personas

| Persona | Needs | Typical action |
|---|---|---|
| Plant / asset data engineer | Which of my 40k tags are stale, stuck, drifting, mis-scaled? | Triages findings, fixes tag configuration, opens tickets. |
| Data scientist / forecaster | Which series can I trust for training and inference? | Filters series by score, exports clean windows, subscribes to drift alerts. |
| O&M / performance engineer (PV, wind) | Are sensors (pyranometers, anemometers, meters) lying? Is the power curve shifting? | Reviews domain findings, schedules calibration. |
| Metering / settlement analyst | Are interval reads complete, plausible and consistent with register reads? | Runs VEE-style checks, estimates gaps, signs off. |
| Platform admin | Who can see what? Are connectors healthy? What did the system do? | Manages users, roles, connectors, audit log. |

## Scope of the first releases

- **R0 (research):** check catalogue, architecture, frontend and security design. *This repo state.*
- **R1 (core):** 30 checks, Parquet/CSV + PI Web API + OPC UA connectors, scoring, dashboard,
  Google login, workspace RBAC, alerts to email/webhook.
- **R2 (energy):** PV, wind, metering and grid check packs; energy-balance checks; estimation/repair.
- **R3 (platform):** custom Python checks, share links, admin panel, audit log, SSO/SCIM.

Out of scope for now: writing corrected data back to source systems, a BPMN/workflow engine
(use Flowable or Temporal externally), and general-purpose BI.
