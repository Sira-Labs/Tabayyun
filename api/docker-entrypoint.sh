#!/bin/sh
# Migrate to the newest schema, then serve. The migration is idempotent, so every container
# start (deploy, restart, scale-out) is safe; a failed migration stops the container before
# uvicorn starts and the previous release keeps serving.
set -eu
python -m tabayyun.db.migrate upgrade head
exec uvicorn tabayyun.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips "*"
