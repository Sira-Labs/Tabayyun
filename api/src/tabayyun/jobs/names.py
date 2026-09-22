"""Queue and task names shared by the API (which enqueues) and the worker (which runs)."""

RUNS_QUEUE = "runs"
MAINTENANCE_QUEUE = "maintenance"
RUN_CHECKS_TASK = "tabayyun.run_checks"
REAP_STALE_RUNS_TASK = "tabayyun.reap_stale_runs"
