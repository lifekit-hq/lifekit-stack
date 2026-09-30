#!/usr/bin/env bash
# docker-prune-policy.sh — the repository's scheduled Docker image + build-cache
# prune: a nightly `docker image prune -af` and `docker builder prune -af`,
# both with a 14-day age floor, run by a systemd timer as root.
#
# Evidence: an ordinary `docker image prune` / `docker builder prune` dry
# reading on the box showed ~93 GB reclaimable (images no container uses,
# build cache nothing has touched in weeks), growing with every on-box build.
#
# How this sits next to scripts/docker-builder-gc.sh: that script caps the
# build cache with dockerd's builder.gc, a SIZE ceiling. The danger it
# documents is a ceiling below the image-shared working set, which makes GC
# evict layers the next build needs on every pass. This prune is the other
# axis, AGE: `until=336h` only touches records and images unused for two
# weeks, and BuildKit refreshes a record's last-used time on every cache hit,
# so anything a build reached in the last 14 days is outside the filter. It
# cannot evict the hot working set; it retires what the cap would otherwise
# keep until the ceiling is hit. The two compose: the cap bounds the worst
# case, this reclaims the long tail. Caveats that follow from the flags:
#   - `image prune -a` skips any image a container (running or stopped)
#     uses, but `until` filters on image CREATION time, not last use: an image
#     built or published more than 14 days ago that no container currently
#     references is removed, and pulled or rebuilt on next use (for example
#     a profile-gated service's image while its container does not exist).
#   - `builder prune -a` removes all unused cache, including records shared
#     with images, once they are 14 days idle; a stage nobody has built in
#     two weeks re-runs on the next build.
#
# Modes:
#   (default)  write the prune copy and units, enable the timer (root;
#              bootstrap-vps.sh runs it). Idempotent. Never prunes.
#   --check    read-only: exit 0 only when the installed copy (same content as
#              this script, executable) and units match and the timer is
#              enabled; 1 on any mismatch. deploy.sh runs it after `up` as a
#              report-only line — the deploy account has no sudo and cannot
#              apply any of this.
#   --prune    run both prunes now and log what each reclaimed (root or a
#              docker-group user; the timer runs it). Exits non-zero when
#              either prune fails, after attempting both.
#
# Nothing here touches /etc/docker/daemon.json or restarts dockerd.
#
# Env overrides (mainly for testing — point these at temp paths to dry-run
# without touching the real host):
#   PRUNE_UNIT_DIR      where the units go (default /etc/systemd/system)
#   PRUNE_BIN           where the prune copy goes (default /usr/local/bin/lifekit-docker-prune.sh)
#   DOCKER_PRUNE_UNTIL  age floor passed as the `until` filter (default below)

set -euo pipefail

# Two weeks. Comfortably longer than the gap between deploys and build-cache
# reuse on this box, so only long-idle state is retired.
DEFAULT_UNTIL=336h

PRUNE_UNIT_DIR="${PRUNE_UNIT_DIR:-/etc/systemd/system}"
PRUNE_BIN="${PRUNE_BIN:-/usr/local/bin/lifekit-docker-prune.sh}"
PRUNE_UNTIL="${DOCKER_PRUNE_UNTIL:-${DEFAULT_UNTIL}}"
PRUNE_UNIT=lifekit-docker-prune
SELF="$(realpath "${BASH_SOURCE[0]}")"

say() { printf '\n\033[1;34m→ %s\033[0m\n' "$*"; }

want_prune_bin() { cat "${SELF}"; }

want_service() {
  cat <<EOF
# Managed by lifekit-stack scripts/docker-prune-policy.sh — do not hand-edit.
[Unit]
Description=Prune Docker images and build cache unused for 14 days

[Service]
Type=oneshot
ExecStart=${PRUNE_BIN} --prune
EOF
}

want_timer() {
  cat <<EOF
# Managed by lifekit-stack scripts/docker-prune-policy.sh — do not hand-edit.
[Unit]
Description=Nightly Docker image and build-cache prune

[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true
RandomizedDelaySec=30m

[Install]
WantedBy=timers.target
EOF
}

MANAGED=(
  "${PRUNE_BIN}:want_prune_bin"
  "${PRUNE_UNIT_DIR}/${PRUNE_UNIT}.service:want_service"
  "${PRUNE_UNIT_DIR}/${PRUNE_UNIT}.timer:want_timer"
)

matches() {
  [[ -f "$1" ]] && diff -q <("$2") "$1" >/dev/null 2>&1 &&
    [[ "$2" != want_prune_bin || -x "$1" ]]
}

check() {
  local mismatch=0 item path gen state
  for item in "${MANAGED[@]}"; do
    path="${item%:*}" gen="${item##*:}"
    if matches "${path}" "${gen}"; then
      echo "  ${path}: matches"
    elif [[ -f "${path}" ]]; then
      echo "  ${path}: present but does not match the repository content"
      mismatch=1
    else
      echo "  ${path}: absent"
      mismatch=1
    fi
  done
  state="$(systemctl is-enabled "${PRUNE_UNIT}.timer" 2>&1 || true)"
  echo "  ${PRUNE_UNIT}.timer: ${state}"
  [[ "${state}" == "enabled" ]] || mismatch=1
  return "${mismatch}"
}

# pipefail makes a failed prune count even though sed ends the pipe. Each prune prints its own "Total reclaimed space" line; the prefix makes the
# journal entry say which half reclaimed what.
prune() {
  local failed=0 kind
  for kind in image builder; do
    echo "docker ${kind} prune -af --filter until=${PRUNE_UNTIL}"
    if ! docker "${kind}" prune -af --filter "until=${PRUNE_UNTIL}" 2>&1 | sed "s/^/  [${kind}] /"; then
      failed=1
    fi
  done
  return "${failed}"
}

case "${1:-}" in
  --check)
    status=0
    check || status=$?
    exit "${status}"
    ;;
  --prune)
    status=0
    prune || status=$?
    exit "${status}"
    ;;
esac

UNITS_CHANGED=0
for item in "${MANAGED[@]}"; do
  path="${item%:*}" gen="${item##*:}"
  if matches "${path}" "${gen}"; then
    say "${path} already matches, skipping"
    continue
  fi
  install -d -m 755 "$(dirname "${path}")"
  tmp="$(mktemp "${path}.XXXXXX")"
  trap 'rm -f "$tmp"' EXIT
  "${gen}" >"$tmp"
  if [[ "${gen}" == want_prune_bin ]]; then chmod 755 "$tmp"; else chmod 644 "$tmp"; fi
  mv "$tmp" "${path}"
  trap - EXIT
  say "Wrote ${path}"
  [[ "${path}" != "${PRUNE_UNIT_DIR}"/* ]] || UNITS_CHANGED=1
done

if [[ "${UNITS_CHANGED}" == 1 ]]; then
  systemctl daemon-reload
fi

if [[ "$(systemctl is-enabled "${PRUNE_UNIT}.timer" 2>&1 || true)" == "enabled" ]]; then
  say "${PRUNE_UNIT}.timer already enabled, skipping"
else
  systemctl enable --now "${PRUNE_UNIT}.timer"
  say "Enabled ${PRUNE_UNIT}.timer"
fi
