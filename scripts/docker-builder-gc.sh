#!/usr/bin/env bash
# docker-builder-gc.sh — cap BuildKit's on-box build cache via dockerd's own
# builder.gc policy, instead of running a periodic prune job. Evidence: 179 GB
# of BuildKit cache accumulated on lifekit-vps from on-box builds by five
# GitHub Actions runners; /etc/docker/daemon.json didn't exist.
#
# Idempotent: merges builder.gc into any existing daemon.json without
# touching unrelated keys (via `jq`), and no-ops (no write) if it's already
# at the desired value.
#
# IMPORTANT: writing this file does not apply it. dockerd only reads
# daemon.json at startup, so applying a change here requires
# `systemctl restart docker`, which restarts EVERY container on the host.
# This script never restarts dockerd — that restart must be scheduled and
# run deliberately, separately from this script.
#
# Env overrides (mainly for testing — point DOCKER_DAEMON_JSON at a temp path
# to dry-run without touching the real host config):
#   DOCKER_DAEMON_JSON           path to daemon.json (default /etc/docker/daemon.json)
#   DOCKER_BUILDER_KEEP_STORAGE  builder.gc.defaultKeepStorage value (default 20GB)

set -euo pipefail

DAEMON_JSON="${DOCKER_DAEMON_JSON:-/etc/docker/daemon.json}"
KEEP_STORAGE="${DOCKER_BUILDER_KEEP_STORAGE:-20GB}"

say() { printf '\n\033[1;34m→ %s\033[0m\n' "$*"; }

if [[ -f "$DAEMON_JSON" ]]; then
  existing="$(cat "$DAEMON_JSON")"
else
  existing="{}"
fi

merged="$(jq --arg keep "$KEEP_STORAGE" \
  '.builder //= {} | .builder.gc //= {} | .builder.gc.enabled = true | .builder.gc.defaultKeepStorage = $keep' \
  <<<"$existing")"

if [[ "$(jq -Sc . <<<"$existing")" == "$(jq -Sc . <<<"$merged")" ]]; then
  say "Docker builder GC cap already configured in $DAEMON_JSON (keep-storage=$KEEP_STORAGE), skipping"
  exit 0
fi

install -d -m 755 "$(dirname "$DAEMON_JSON")"
tmp="$(mktemp "${DAEMON_JSON}.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
jq . <<<"$merged" >"$tmp"
chmod 644 "$tmp"
mv "$tmp" "$DAEMON_JSON"
trap - EXIT

say "Wrote Docker builder GC cap (keep-storage=$KEEP_STORAGE) to $DAEMON_JSON"
cat <<EOF

NOTE: this only takes effect after dockerd re-reads its config. Apply with:

    systemctl restart docker

This restarts EVERY container on the host. Do not run that here — schedule
the restart deliberately (e.g. a maintenance window) and run it by hand.

EOF
