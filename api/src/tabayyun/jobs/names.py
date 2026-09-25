"""Queue and task names shared by the API (which enqueues) and the worker (which runs)."""

RUNS_QUEUE = "runs"
MAINTENANCE_QUEUE = "maintenance"
RUN_CHECKS_TASK = "tabayyun.run_checks"
REAP_STALE_RUNS_TASK = "tabayyun.reap_stale_runs"
# Postgres application_name prefix of the worker's connections; the suffix after "/" is the
# commit it runs, which /api/version lists (see `worker_application_name`).
WORKER_APPLICATION_NAME = "tabayyun-worker"
