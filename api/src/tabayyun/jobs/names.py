"""Queue and task names shared by the API (which enqueues) and the worker (which runs)."""

RUNS_QUEUE = "runs"
MAINTENANCE_QUEUE = "maintenance"
MAIL_QUEUE = "mail"
FETCH_QUEUE = "fetch"
RUN_CHECKS_TASK = "tabayyun.run_checks"
REAP_STALE_RUNS_TASK = "tabayyun.reap_stale_runs"
SEND_INVITATION_TASK = "tabayyun.send_invitation_email"
PRUNE_RATE_LIMITS_TASK = "tabayyun.prune_rate_limits"
FETCH_WINDOW_TASK = "tabayyun.fetch_window"
CHECK_SOURCE_TASK = "tabayyun.check_source"
POLL_SOURCES_TASK = "tabayyun.poll_sources"
PRUNE_SOURCE_FETCHES_TASK = "tabayyun.prune_source_fetches"
# Postgres application_name prefix of the worker's connections; the suffix after "/" is the
# commit it runs, which /api/version lists (see `worker_application_name`).
WORKER_APPLICATION_NAME = "tabayyun-worker"
