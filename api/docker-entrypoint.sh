#!/bin/sh
# One image, two roles, chosen by TABAYYUN_ROLE (default api):
#   api     migrate to the newest schema (idempotent, safe on every start), then serve.
#   worker  refuse to start until the schema is at head (exit 3, restart until the api has
#           migrated), then run the Procrastinate worker on every queue.
# A failed migration stops the new api container before uvicorn starts, so the previous
# release keeps serving. `tabayyun-api healthcheck` is the image's HEALTHCHECK: the api
# role probes /healthz, the worker role is healthy while its process runs.
set -eu
role="${TABAYYUN_ROLE:-api}"
if [ "${1:-}" = "healthcheck" ]; then
  [ "$role" = worker ] && exit 0
  exec python -c 'import sys, urllib.request
try:
    sys.exit(0 if urllib.request.urlopen("http://127.0.0.1:8000/healthz", timeout=2).status == 200 else 1)
except OSError:
    sys.exit(1)'
fi
case "$role" in
  api)
    python -m tabayyun.db.migrate upgrade head
    exec uvicorn tabayyun.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips "*"
    ;;
  worker)
    exec python -m tabayyun.jobs
    ;;
  *)
    echo "TABAYYUN_ROLE must be api or worker, not '$role'" >&2
    exit 2
    ;;
esac
