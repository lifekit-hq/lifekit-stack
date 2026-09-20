#!/usr/bin/env bash
# docker-builder-gc.sh — cap BuildKit's on-box build cache via dockerd's own
# builder.gc policy, instead of running a periodic prune job. Evidence: 179 GB
# of BuildKit cache accumulated on lifekit-vps from on-box builds by five
# GitHub Actions runners; /etc/docker/daemon.json didn't exist.
#
# Two modes:
#   (default)  merge builder.gc into daemon.json (root; bootstrap-vps.sh runs it)
#   --check    read-only: compare the ceiling the running daemon enforces with
#              the value below. Exit 0 match, 1 mismatch, 2 undetermined (the
#              live policy could not be read). deploy.sh runs it after `up`, as
#              the deploy account, and prints the result as a report-only line
#              — the deploy cannot apply this cap, see below.
#
# Why 50GB (retuned from 20GB on 2026-09-20): with no daemon.json the ceiling is
# BuildKit's disk-scaled default Max Used Space — 375.3 GiB on this 503 GB box —
# which is why the cache reached 67.66 GB unchecked. Of that, 26.3 GB was cache
# shared with image layers (52 images, 88 GB) and the rest private, dangling
# records that a manual prune reclaimed. A cap below the image-shared working
# set (the old 20GB) makes GC evict live layer references on every pass, so
# every build re-runs stages that already exist; 50GB keeps that ~27 GB set
# plus ~23 GB of intermediate cache between GC passes (the cache grew ~10 GB in
# the 3 days before 2026-09-20) and is 10% of the disk. Note dockerd parses
# "50GB" with go-units RAM semantics, i.e. as 50 GiB ≈ 53.7 GB, so the steady
# state sits only ~14 GB below the 67.66 GB that motivated this work — that is
# deliberate, not slack: anything nearer the ~27 GB image-shared set trades the
# disk back for re-running build stages on every deploy.
#
# Semantics (moby daemon/internal/builder-next/worker/gc.go + BuildKit
# cache/manager.go, verified against Engine 29.5.2 on this box): a GC rule
# carries a reserved space (what GC never reclaims below) and a max used space
# (the ceiling whose breach triggers a prune) as independent knobs, and dockerd
# fills both from disk-scaled defaults when daemon.json is silent — here
# Reserved Space 47.5 GiB, Max Used Space 375.3 GiB, Min Free Space 94.06 GiB.
# Only the ceiling bounds the cache: the deprecated defaultKeepStorage is an
# alias for the reserved value alone, so setting it would have raised the floor
# 47.5 GiB -> 50 GiB and capped nothing — dockerd had run since 2026-06-12 with
# that floor while the cache grew to 67.66 GB. Hence defaultReservedSpace and
# defaultMaxUsedSpace, both at the value below: GC triggers at 50 GiB and prunes
# back to 50 GiB. `docker buildx inspect default` prints the ceiling as the
# "Max Used Space" of the last (All: true) rule — the field --check compares.
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
#   DOCKER_DAEMON_JSON         path to daemon.json (default /etc/docker/daemon.json)
#   DOCKER_BUILDER_CACHE_CAP   the cap written to builder.gc (default below)

set -euo pipefail

DEFAULT_CACHE_CAP=50GB

DAEMON_JSON="${DOCKER_DAEMON_JSON:-/etc/docker/daemon.json}"
CACHE_CAP="${DOCKER_BUILDER_CACHE_CAP:-${DEFAULT_CACHE_CAP}}"

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

UNDETERMINED=2

check() {
  local inspect want live want_b live_b
  if [[ -f "${DAEMON_JSON}" ]]; then
    echo "  ${DAEMON_JSON}: builder.gc = $(jq -c '.builder.gc // "absent"' "${DAEMON_JSON}" 2>/dev/null || echo unreadable)"
  else
    echo "  ${DAEMON_JSON}: absent"
  fi
  want="${CACHE_CAP}"
  if ! inspect="$(docker buildx inspect default 2>&1)"; then
    echo "  live daemon policy: \`docker buildx inspect default\` failed: ${inspect}"
    return "${UNDETERMINED}"
  fi
  # Only the last (All: true) rule's Max Used Space is the ceiling that bounds
  # the cache; the earlier, filtered rules cap unrelated subsets. Scope the
  # read to that rule, so a policy where it carries no ceiling reads as
  # undetermined instead of as some other rule's number.
  live="$(awk '
    /^[[:space:]]*All:/ { in_all = ($2 == "true"); if (in_all) v = ""; next }
    in_all && /^[[:space:]]*Max Used Space:/ { v = $4 }
    END { print v }' <<<"${inspect}")"
  echo "  live daemon policy (docker buildx inspect default): max used space = ${live:-unreadable}; repository cap = ${want}"
  if [[ -z "${live}" ]]; then
    return "${UNDETERMINED}"
  fi
  want_b="$(to_bytes "${want}")"
  if ! live_b="$(to_bytes "${live}")"; then
    return "${UNDETERMINED}"
  fi
  # buildx prints 2 decimals (46.57GiB, 47.5GiB), so allow 1% rounding.
  awk -v w="${want_b}" -v l="${live_b}" 'BEGIN { d = w - l; if (d < 0) d = -d; exit !(d <= w / 100) }'
}

if [[ "${1:-}" == "--check" ]]; then
  status=0
  check || status=$?
  exit "${status}"
fi

if [[ -f "$DAEMON_JSON" ]]; then
  existing="$(cat "$DAEMON_JSON")"
else
  existing="{}"
fi

merged="$(jq --arg cap "$CACHE_CAP" \
  '.builder //= {} | .builder.gc //= {}
   | .builder.gc.enabled = true
   | .builder.gc.defaultReservedSpace = $cap
   | .builder.gc.defaultMaxUsedSpace = $cap
   | del(.builder.gc.defaultKeepStorage)' \
  <<<"$existing")"

if [[ "$(jq -Sc . <<<"$existing")" == "$(jq -Sc . <<<"$merged")" ]]; then
  say "Docker builder GC cap already configured in $DAEMON_JSON (cap=$CACHE_CAP), skipping"
  exit 0
fi

install -d -m 755 "$(dirname "$DAEMON_JSON")"
tmp="$(mktemp "${DAEMON_JSON}.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
jq . <<<"$merged" >"$tmp"
chmod 644 "$tmp"
mv "$tmp" "$DAEMON_JSON"
trap - EXIT

say "Wrote Docker builder GC cap (cap=$CACHE_CAP) to $DAEMON_JSON"
cat <<EOF

NOTE: this only takes effect after dockerd re-reads its config. Apply with:

    systemctl restart docker

This restarts EVERY container on the host. Do not run that here — schedule
the restart deliberately (e.g. a maintenance window) and run it by hand.
Confirm afterwards with:  bash scripts/docker-builder-gc.sh --check
"docker buildx inspect default" must then show Max Used Space at the cap,
not the disk-scaled 375.3GiB default.

EOF
