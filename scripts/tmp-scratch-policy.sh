#!/usr/bin/env bash
# tmp-scratch-policy.sh — the repository's half of moving /tmp off a
# RAM-backed filesystem: mask the distro's default tmpfs-on-/tmp mount unit
# and tighten how fast stale entries age out, so scratch that used to build
# up on RAM-backed /tmp instead lands on disk and gets swept on a short
# schedule instead of accumulating until something breaks.
#
# Evidence: a RAM-backed /tmp filled to capacity from build/session scratch
# left behind by finished tasks, which both broke any host command that needs
# a temp file and quietly ate into memory being rationed elsewhere. Clearing
# the leftovers was remediation, not a fix — the underlying filesystem was
# still RAM, and nothing retired a task's scratch once the task ended.
#
# The fix has two independent halves, same shape as scripts/docker-builder-gc.sh:
#   1. `systemctl mask tmp.mount` — stops systemd from remounting /tmp as
#      tmpfs. /tmp then falls back to being an ordinary directory on the root
#      filesystem, where the disk has room. Masking a unit only prevents
#      future (re)starts; it does not by itself unmount an already-active
#      tmpfs, so on a box where tmp.mount is currently mounted this alone
#      does not move existing scratch off RAM until the next boot or an
#      explicit `systemctl stop tmp.mount`.
#   2. /etc/tmpfiles.d/lifekit-tmp-scratch.conf — overrides the distro
#      default (/usr/lib/tmpfiles.d/tmp.conf ages /tmp out after 10 days) with
#      a much shorter age, so scratch left behind by a finished task is swept
#      by the next systemd-tmpfiles-clean.timer pass rather than sitting
#      until it becomes a capacity incident. This is a real file under
#      /etc/tmpfiles.d, not a new timer: the clean timer already ships and
#      runs on its own schedule, and systemd-tmpfiles(5) documents this exact
#      override pattern ("Clear tmp directories separately, to make them
#      easier to override").
#
# Neither half touches Docker, any container, or any other daemon: masking a
# mount unit and writing a tmpfiles.d drop-in take effect on their own
# schedule (next mount attempt / next tmpfiles-clean pass) without a restart.
# Making the change take effect immediately instead of on that schedule is a
# deliberate operator sequence — see docs/runbook.md "Moving /tmp off RAM
# (agent scratch)" — because live-unmounting an active tmpfs while things
# have open files under it is not something to do unattended.
#
# Two modes:
#   (default)  write the tmpfiles.d drop-in and mask tmp.mount (root;
#              bootstrap-vps.sh runs it). Idempotent — no-ops if both are
#              already applied.
#   --check    read-only: report whether the drop-in is present with the
#              expected age and whether tmp.mount is masked, and separately
#              whether /tmp is *currently* still mounted as tmpfs (which can
#              be true even once masked, until the deliberate live cutover in
#              the runbook). Exit 0 fully converged, 1 mismatch, 2
#              undetermined. deploy.sh runs this after `up` and prints the
#              result as a report-only line — the deploy account has no sudo
#              and cannot apply either half.
#
# Env overrides (mainly for testing — point these at temp paths to dry-run
# without touching the real host config):
#   TMPFILES_DROPIN   path to the drop-in (default /etc/tmpfiles.d/lifekit-tmp-scratch.conf)
#   TMP_SCRATCH_AGE   age written into the drop-in (default below)

set -euo pipefail

DEFAULT_AGE=1d

TMPFILES_DROPIN="${TMPFILES_DROPIN:-/etc/tmpfiles.d/lifekit-tmp-scratch.conf}"
AGE="${TMP_SCRATCH_AGE:-${DEFAULT_AGE}}"

say() { printf '\n\033[1;34m→ %s\033[0m\n' "$*"; }

want_dropin() {
  cat <<EOF
# Managed by lifekit-stack scripts/tmp-scratch-policy.sh — do not hand-edit.
# Overrides /usr/lib/tmpfiles.d/tmp.conf's 10-day default: retires
# leftover agent/task scratch on a much shorter horizon than a RAM-backed
# /tmp can survive unnoticed. See systemd-tmpfiles.d(5) and docs/runbook.md
# "Moving /tmp off RAM (agent scratch)".
q /tmp 1777 root root ${AGE}
EOF
}

UNDETERMINED=2

check() {
  local dropin_ok=1 mask_ok=1 live_tmpfs
  if [[ -f "${TMPFILES_DROPIN}" ]]; then
    if diff -q <(want_dropin) "${TMPFILES_DROPIN}" >/dev/null 2>&1; then
      echo "  ${TMPFILES_DROPIN}: matches (age=${AGE})"
    else
      echo "  ${TMPFILES_DROPIN}: present but does not match the repository content (age=${AGE})"
      dropin_ok=0
    fi
  else
    echo "  ${TMPFILES_DROPIN}: absent"
    dropin_ok=0
  fi

  local mask_state
  mask_state="$(systemctl is-enabled tmp.mount 2>&1 || true)"
  echo "  tmp.mount unit state: ${mask_state}"
  if [[ "${mask_state}" != "masked" ]]; then
    mask_ok=0
  fi

  live_tmpfs="$(findmnt -no FSTYPE /tmp 2>/dev/null || echo unknown)"
  echo "  /tmp live filesystem: ${live_tmpfs} (masking tmp.mount only prevents future mounts;"
  echo "    an already-active tmpfs mount needs the live cutover in docs/runbook.md to clear now)"

  if [[ "${dropin_ok}" == 1 && "${mask_ok}" == 1 ]]; then
    return 0
  fi
  if [[ "${live_tmpfs}" == "unknown" ]]; then
    return "${UNDETERMINED}"
  fi
  return 1
}

if [[ "${1:-}" == "--check" ]]; then
  status=0
  check || status=$?
  exit "${status}"
fi

CHANGED=0

if [[ -f "${TMPFILES_DROPIN}" ]] && diff -q <(want_dropin) "${TMPFILES_DROPIN}" >/dev/null 2>&1; then
  say "${TMPFILES_DROPIN} already matches (age=${AGE}), skipping"
else
  install -d -m 755 "$(dirname "${TMPFILES_DROPIN}")"
  tmp="$(mktemp "${TMPFILES_DROPIN}.XXXXXX")"
  trap 'rm -f "$tmp"' EXIT
  want_dropin >"$tmp"
  chmod 644 "$tmp"
  mv "$tmp" "${TMPFILES_DROPIN}"
  trap - EXIT
  say "Wrote ${TMPFILES_DROPIN} (age=${AGE})"
  CHANGED=1
fi

if [[ "$(systemctl is-enabled tmp.mount 2>&1 || true)" == "masked" ]]; then
  say "tmp.mount already masked, skipping"
else
  systemctl mask tmp.mount
  say "Masked tmp.mount"
  CHANGED=1
fi

if [[ "${CHANGED}" == 1 ]]; then
  cat <<EOF

NOTE: neither change is live yet. Masking tmp.mount only stops FUTURE mounts,
and the tmpfiles.d age only applies on the next scheduled
systemd-tmpfiles-clean.timer pass. Making both effective now instead of on
their own schedule is a deliberate operator sequence — see docs/runbook.md
"Moving /tmp off RAM (agent scratch)". This script never unmounts /tmp or
runs systemd-tmpfiles itself.

EOF
fi
