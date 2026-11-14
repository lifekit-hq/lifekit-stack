#!/usr/bin/env bash
# Run one OpenClaw API spend-guard pass and write openclaw_api_spend.prom into
# node-exporter's textfile directory. Installed to /usr/local/bin by
# scripts/host-gauge/install-host-gauges.sh and run every 10 minutes by
# lifekit-openclaw-spend-guard.timer as the admin account (docker group, owns
# the textfile directory). The logic lives in the deployed checkout so a merge
# updates it without reinstalling: scripts/openclaw-auth/spend_guard.py.
# docs/runbook.md "OpenClaw auth mode".
set -euo pipefail
exec python3 "${SPEND_GUARD:-/srv/lifekit-stack/scripts/openclaw-auth/spend_guard.py}" run \
  --textfile "${1:-/var/lib/node_exporter/textfile}" \
  --state-dir "${OPENCLAW_SPEND_STATE_DIR:-/var/lib/lifekit-openclaw-spend}"
