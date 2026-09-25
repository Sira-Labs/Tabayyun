#!/usr/bin/env bash
# Wait until a deployment serves the wanted commit (release.yml and promote.yml).
#
# CapRover accepts a deploy before it pulls the image, so a failed pull or a container that
# never starts would otherwise leave the job green. Polls <URL>/version.json (web image) and
# <URL>/api/version (api image) and, with CHECK_WORKER=true, waits until /api/version lists a
# connected worker on the commit (the worker names its database connections after it).
#
#   URL=https://tabayyun-stg.siralabs.org WANT=<full sha> CHECK_WORKER=true wait-live.sh
# URL unset: prints a notice and succeeds. TIMEOUT (seconds, default 600) and INTERVAL (15).
set -euo pipefail

if [ -z "${URL:-}" ]; then
  echo "::notice::no public URL configured; not checking that the deploy went live"
  exit 0
fi
URL="${URL%/}"
: "${WANT:?WANT (the commit to wait for) is required}"
deadline=$((SECONDS + ${TIMEOUT:-600}))
while :; do
  web=$(curl -fsS -m 10 "$URL/version.json" 2>/dev/null | jq -r '.commit // empty' 2>/dev/null || true)
  version=$(curl -fsS -m 10 "$URL/api/version" 2>/dev/null || true)
  api=$(jq -r '.commit // empty' <<< "$version" 2>/dev/null || true)
  workers=$(jq -r '(.workers // []) | join(",")' <<< "$version" 2>/dev/null || true)
  worker_ok=true
  if [ "${CHECK_WORKER:-false}" = "true" ] && [[ ",$workers," != *",$WANT,"* ]]; then worker_ok=false; fi
  if [ "$web" = "$WANT" ] && [ "$api" = "$WANT" ] && [ "$worker_ok" = "true" ]; then
    if [ "${CHECK_WORKER:-false}" = "true" ]; then echo "web, api and worker run $WANT"; else echo "web and api run $WANT"; fi
    exit 0
  fi
  if [ "$SECONDS" -ge "$deadline" ]; then
    waited=$(( ${TIMEOUT:-600} / 60 ))
    echo "::error::after ${waited} minutes $URL serves web '${web:-?}', api '${api:-?}', workers '${workers:-none}', not $WANT; see the apps' deployment logs in CapRover"
    exit 1
  fi
  sleep "${INTERVAL:-15}"
done
