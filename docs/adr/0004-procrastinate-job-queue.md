# ADR-0004: Procrastinate (Postgres-backed) for scheduling and jobs

- **Status:** Accepted
- **Date:** 2026-09-21

## Context
Runs, fetches, alert deliveries and plugin executions are background jobs. Air-gapped
installs should not require Redis or RabbitMQ.

## Decision
Use Procrastinate: Postgres LISTEN/NOTIFY, `FOR UPDATE SKIP LOCKED`, periodic tasks,
transactional enqueue with the run record. Workers are plain Python processes; a dedicated
worker class hosts sandboxed plugin checks.

## Alternatives considered
| Option | Pros | Cons | Why not |
|---|---|---|---|
| Celery / Dramatiq / Taskiq | Mature | Need a broker | Extra infrastructure |
| Temporal / Hatchet | Durable execution, visibility | Another server and schema to air-gap | Revisit for very long resumable runs |
| APScheduler in-process | Simple | No queue, no retries across processes | Not enough |

## Consequences
Queue throughput is bounded by Postgres; adequate for thousands of jobs per minute, which is
far above the expected load. Migration path to Hatchet exists if needed.
