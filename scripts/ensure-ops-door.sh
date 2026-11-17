#!/usr/bin/env bash
# ensure-ops-door.sh - assert the ops door is live: the receiver container
# (compose/ops-door/) is up, /ready answers 200 (the GitHub token is placed and
# GitHub accepts it) and its escalation spool is writable by the container.
# docs/runbook.md "Ops door".
#
# deploy.sh runs it after the platform `up` and turns the deploy red on a
# non-zero exit. It never prints the token, never reads a secret file, and
# changes nothing: placing the token and installing the host timer are
# operator steps (runbook), reported by install-ops-door.sh --check.
#
# Exit: 0 live; 1 not live (the reason is on stderr).
#
# Env overrides:
#   OPS_DOOR_PROJECT   compose project (default: compose)
#   OPS_DOOR_WAIT      seconds to wait for /ready (default: 40)
set -euo pipefail

PROJECT="${OPS_DOOR_PROJECT:-compose}"
WAIT="${OPS_DOOR_WAIT:-40}"

CONTAINER="$(docker ps -q --filter "label=com.docker.compose.project=${PROJECT}" \
  --filter label=com.docker.compose.service=ops-door 2>/dev/null | head -n 1 || true)"
if [ -z "$CONTAINER" ]; then
  echo "ops-door: no running container in compose project '${PROJECT}'" >&2
  exit 1
fi

# shellcheck disable=SC2016  # JavaScript, not shell
probe='fetch("http://127.0.0.1:8097/ready").then((r) => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))'
deadline=$((SECONDS + WAIT))
until docker exec "$CONTAINER" node -e "$probe" >/dev/null 2>&1; do
  if (( SECONDS >= deadline )); then
    echo "ops-door: /ready did not answer 200 within ${WAIT}s - the GitHub token (OPS_DOOR_GITHUB_TOKEN) is missing, or GitHub refuses it; see docs/runbook.md \"Ops door\"" >&2
    exit 1
  fi
  sleep 2
done
echo "ops-door: ready (GitHub reachable with the placed token)"

# shellcheck disable=SC2016  # JavaScript, not shell
if ! docker exec "$CONTAINER" node -e 'require("fs").accessSync("/spool", require("fs").constants.W_OK)' >/dev/null 2>&1; then
  echo "ops-door: /spool is not writable by the container - run scripts/ops-door/install-ops-door.sh as root (docs/runbook.md \"Ops door\")" >&2
  exit 1
fi
echo "ops-door: escalation spool writable"
