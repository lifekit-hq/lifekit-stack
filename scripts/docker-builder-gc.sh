#!/usr/bin/env bash
# docker-builder-gc.sh — cap BuildKit's on-box build cache via dockerd's own
# builder.gc policy, instead of running a periodic prune job. Evidence: 179 GB
# of BuildKit cache accumulated on lifekit-vps from on-box builds by five
# GitHub Actions runners; /etc/docker/daemon.json didn't exist.
#
# Two modes:
#   (default)  merge builder.gc into daemon.json (root; bootstrap-vps.sh runs it)
#   --check    read-only: compare the cap the running daemon enforces with the
#              value below and exit 1 on mismatch (deploy.sh runs it after `up`,
#              as the deploy account, and prints the result as a report-only
#              line — the deploy cannot apply this cap, see below)
#
# Why 50GB (retuned from 20GB on 2026-09-20): without a daemon.json the cap is
# BuildKit's disk-scaled default — 80% of the disk, 375 GiB on this 503 GB box —
# which is why the cache reached 67.66 GB unchecked. Of that, 26.3 GB was cache
# shared with image layers (52 images, 88 GB) and the rest private, dangling
# records that a manual prune reclaimed. A cap below the image-shared working
# set (the old 20GB) makes GC evict live layer references on every pass, so
# every build re-runs stages that already exist; 50GB keeps that ~27 GB set
# plus ~23 GB of intermediate cache between GC passes (the cache grew ~10 GB in
# the 3 days before 2026-09-20) and is 10% of the disk.
#
# Semantics (moby daemon/internal/builder-next/worker/gc.go + BuildKit
# cache/manager.go, verified against Engine 29.5.2): the daemon reads
# defaultKeepStorage as the reserved space of its default 4-rule policy; with
# max-used and min-free unset, BuildKit prunes down to exactly that value once
# the cache exceeds it. `docker buildx inspect default` shows it as the
# "Reserved Space" of the last (All: true) rule.
#
# Idempotent: merges builder.gc into any existing daemon.json without
# touching unrelated keys (via `jq`), and no-ops (no write) if it's already
# at the desired value.
#
# IMPORTANT: writing this file does not apply it. dockerd only reads
# daemon.json at startup (builder.* is not on its SIGHUP reload list), so
# applying a change here requires `systemctl restart docker`, which restarts
# EVERY container on the host — this stack's services run with
# restart: on-failure and do not come back on their own, and the other
# projects on the box (finance-sentry, devclaw, dashboard, xui, closeloop)
# go down with them. This script never restarts dockerd — that restart must
# be scheduled and run deliberately, separately from this script. The deploy
# account has no sudo, so deploy.sh only checks (--check) and never writes.
#
# Env overrides (mainly for testing — point DOCKER_DAEMON_JSON at a temp path
# to dry-run without touching the real host config):
#   DOCKER_DAEMON_JSON           path to daemon.json (default /etc/docker/daemon.json)
#   DOCKER_BUILDER_KEEP_STORAGE  builder.gc.defaultKeepStorage value (default below)
#   DOCKER_BUILDX_INSPECT        --check only: file with `docker buildx inspect
#                                default` output to parse instead of running it

set -euo pipefail

DEFAULT_KEEP_STORAGE=50GB

DAEMON_JSON="${DOCKER_DAEMON_JSON:-/etc/docker/daemon.json}"
KEEP_STORAGE="${DOCKER_BUILDER_KEEP_STORAGE:-${DEFAULT_KEEP_STORAGE}}"

say() { printf '\n\033[1;34m→ %s\033[0m\n' "$*"; }

# Size string -> bytes, the way dockerd parses daemon.json sizes
# (go-units RAMInBytes: GB and GiB are both 1024^3) and the way
# `docker buildx inspect` prints them (46.57GiB, 512MB, ...).
to_bytes() {
  awk -v s="$1" 'BEGIN {
    if (match(s, /^[0-9.]+/) == 0) { exit 1 }
    n = substr(s, 1, RLENGTH); u = toupper(substr(s, RLENGTH + 1))
    sub(/^ +/, "", u); sub(/IB$/, "B", u)
    p = index("BKMGTP", substr(u, 1, 1)) - 1
    if (u == "" ) { p = 0 } else if (p < 0) { exit 1 }
    printf "%.0f\n", n * (1024 ^ p)
  }'
}

check() {
  local inspect want live want_b live_b
  if [[ -n "${DOCKER_BUILDX_INSPECT:-}" ]]; then
    inspect="$(cat "${DOCKER_BUILDX_INSPECT}")"
  else
    inspect="$(docker buildx inspect default)"
  fi
  # The last rule of dockerd's policy is the All: true one; its Reserved
  # Space is what BuildKit prunes down to.
  live="$(awk '/^ *Reserved Space:/ { v = $3 } END { print v }' <<<"${inspect}")"
  want="${KEEP_STORAGE}"
  if [[ -f "${DAEMON_JSON}" ]]; then
    echo "  ${DAEMON_JSON}: builder.gc = $(jq -c '.builder.gc // "absent"' "${DAEMON_JSON}" 2>/dev/null || echo unreadable)"
  else
    echo "  ${DAEMON_JSON}: absent"
  fi
  echo "  live daemon policy (docker buildx inspect default): reserved space = ${live:-unknown}; repository cap = ${want}"
  want_b="$(to_bytes "${want}")"
  live_b="$(to_bytes "${live:-0}")" || live_b=0
  # buildx prints 2 decimals (46.57GiB, 47.5GiB), so allow 1% rounding.
  awk -v w="${want_b}" -v l="${live_b}" 'BEGIN { d = w - l; if (d < 0) d = -d; exit !(d <= w / 100) }'
}

if [[ "${1:-}" == "--check" ]]; then
  check
  exit
fi

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
cat <<EOF2

NOTE: this only takes effect after dockerd re-reads its config. Apply with:

    systemctl restart docker

This restarts EVERY container on the host. Do not run that here — schedule
the restart deliberately (e.g. a maintenance window) and run it by hand.
Confirm afterwards with:  bash scripts/docker-builder-gc.sh --check

EOF2
