#!/usr/bin/env bash
# tmp-scratch-policy.sh — the repository's half of moving /tmp off a
# RAM-backed filesystem: mask the distro's default tmpfs-on-/tmp mount unit
# so scratch lands on disk, and retire scratch that finished tasks leave
# behind with a sweep that never removes anything still in use.
#
# Evidence: a RAM-backed /tmp filled to capacity from build/session scratch
# left behind by finished tasks, which both broke any host command that needs
# a temp file and quietly ate into memory being rationed elsewhere. Clearing
# the leftovers was remediation, not a fix — the underlying filesystem was
# still RAM, and nothing retired a task's scratch once the task ended.
#
# What this repository owns, and what it does not: it controls WHERE scratch
# lives and can safely reclaim ABANDONED scratch. It does not know when an
# agent task ends — that signal belongs to the supervisor that starts and
# ends tasks, and end-of-task retirement is follow-up work owned there. The
# sweep below is the host-side backstop, not a task-lifecycle hook.
#
# The policy, same shape as scripts/docker-builder-gc.sh:
#   1. `systemctl mask tmp.mount` — stops systemd from remounting /tmp as
#      tmpfs. /tmp then falls back to being an ordinary directory on the root
#      filesystem, where the disk has room. Masking a unit only prevents
#      future (re)starts; it does not by itself unmount an already-active
#      tmpfs, so on a box where tmp.mount is currently mounted this alone
#      does not move existing scratch off RAM until the next boot or an
#      explicit `systemctl stop tmp.mount`.
#   2. /etc/tmpfiles.d/lifekit-tmp-scratch.conf — overrides the distro
#      /usr/lib/tmpfiles.d/tmp.conf line for /tmp with no age, so
#      systemd-tmpfiles-clean never deletes /tmp entries on age alone (it
#      cannot tell whether a live task still uses them).
#   3. lifekit-tmp-scratch-sweep.{service,timer} — a daily `--sweep`. Liveness
#      gates deletion: a top-level /tmp entry is removed only when no running
#      process has a file under it open, as its working directory, or as its
#      executable, and it holds no socket. Age only narrows the candidates.
#
# Nothing here unmounts /tmp or restarts a daemon. Moving an already-mounted
# tmpfs /tmp onto disk now instead of at next boot is a deliberate operator
# sequence — see docs/runbook.md "Moving /tmp off RAM (agent scratch)" —
# because unmounting it discards everything on it.
#
# Modes:
#   (default)  write the drop-in and sweep units, enable the timer, mask
#              tmp.mount (root; bootstrap-vps.sh runs it). Idempotent.
#   --check    read-only: exit 0 only when the drop-in and units match, the
#              timer is enabled, tmp.mount is masked AND /tmp is not live on
#              tmpfs; 1 on any known mismatch (a still-live tmpfs /tmp is a
#              mismatch — it is the incident state); 2 when the live /tmp
#              filesystem cannot be read and nothing else is known to be
#              wrong. deploy.sh runs it after `up` as a report-only line —
#              the deploy account has no sudo and cannot apply any of this.
#   --sweep    remove abandoned top-level entries of /tmp (root; the timer
#              runs it). Exits 2 without removing anything if it cannot read
#              every process's open files — a partial view is not proof that
#              nothing holds an entry.
#
# Env overrides (mainly for testing — point these at temp paths to dry-run
# without touching the real host):
#   TMPFILES_DROPIN        drop-in path (default /etc/tmpfiles.d/lifekit-tmp-scratch.conf)
#   SWEEP_UNIT_DIR         where the sweep units go (default /etc/systemd/system)
#   SCRATCH_ROOT           the directory checked and swept (default /tmp)
#   PROC_ROOT              process table the sweep reads (default /proc)
#   TMP_SCRATCH_AGE_DAYS   sweep backstop age (default below)

set -euo pipefail

# A week: far beyond any build or agent session this box runs (those finish
# within hours), so an entry where nothing has been written or created for
# that long (mtime and ctime both — tar restores old mtimes) and that nothing
# holds open is abandoned, not paused. The liveness check, not this age, is
# what protects a running task's scratch.
DEFAULT_AGE_DAYS=7

TMPFILES_DROPIN="${TMPFILES_DROPIN:-/etc/tmpfiles.d/lifekit-tmp-scratch.conf}"
SWEEP_UNIT_DIR="${SWEEP_UNIT_DIR:-/etc/systemd/system}"
SCRATCH_ROOT="${SCRATCH_ROOT:-/tmp}"
PROC_ROOT="${PROC_ROOT:-/proc}"
AGE_DAYS="${TMP_SCRATCH_AGE_DAYS:-${DEFAULT_AGE_DAYS}}"
SWEEP_UNIT=lifekit-tmp-scratch-sweep
SELF="$(realpath "${BASH_SOURCE[0]}")"
UNDETERMINED=2

say() { printf '\n\033[1;34m→ %s\033[0m\n' "$*"; }

want_dropin() {
  cat <<EOF
# Managed by lifekit-stack scripts/tmp-scratch-policy.sh — do not hand-edit.
# Overrides /usr/lib/tmpfiles.d/tmp.conf's age for /tmp: no age-only cleanup.
# Abandoned scratch is retired by ${SWEEP_UNIT}.timer, which skips anything
# still in use. See docs/runbook.md "Moving /tmp off RAM (agent scratch)".
q /tmp 1777 root root -
EOF
}

want_service() {
  cat <<EOF
# Managed by lifekit-stack scripts/tmp-scratch-policy.sh — do not hand-edit.
[Unit]
Description=Retire abandoned /tmp scratch that nothing holds open

[Service]
Type=oneshot
ExecStart=/bin/bash ${SELF} --sweep
EOF
}

want_timer() {
  cat <<EOF
# Managed by lifekit-stack scripts/tmp-scratch-policy.sh — do not hand-edit.
[Unit]
Description=Daily sweep of abandoned /tmp scratch

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=1h

[Install]
WantedBy=timers.target
EOF
}

MANAGED=(
  "${TMPFILES_DROPIN}:want_dropin"
  "${SWEEP_UNIT_DIR}/${SWEEP_UNIT}.service:want_service"
  "${SWEEP_UNIT_DIR}/${SWEEP_UNIT}.timer:want_timer"
)

matches() { [[ -f "$1" ]] && diff -q <("$2") "$1" >/dev/null 2>&1; }

check() {
  local mismatch=0 item path gen state live_fs
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

  state="$(systemctl is-enabled "${SWEEP_UNIT}.timer" 2>&1 || true)"
  echo "  ${SWEEP_UNIT}.timer: ${state}"
  [[ "${state}" == "enabled" ]] || mismatch=1

  state="$(systemctl is-enabled tmp.mount 2>&1 || true)"
  echo "  tmp.mount unit state: ${state}"
  [[ "${state}" == "masked" ]] || mismatch=1

  live_fs="$(findmnt -no FSTYPE -T "${SCRATCH_ROOT}" 2>/dev/null || true)"
  echo "  ${SCRATCH_ROOT} live filesystem: ${live_fs:-unknown}"
  if [[ "${live_fs}" == "tmpfs" ]]; then
    echo "    still RAM-backed: masking tmp.mount only prevents future mounts; the live"
    echo "    cutover in docs/runbook.md moves it onto disk now"
    mismatch=1
  fi

  if [[ "${mismatch}" == 1 ]]; then
    return 1
  fi
  if [[ -z "${live_fs}" ]]; then
    return "${UNDETERMINED}"
  fi
  return 0
}

# dev:inode of everything a running process holds: open files, working
# directory, executable. Fails if any live process's fd table is unreadable.
held_inodes() {
  local p
  for p in "${PROC_ROOT}"/[0-9]*/; do
    p="${p%/}"
    if [[ ! -r "${p}/fd" || ! -x "${p}/fd" ]]; then
      [[ -e "${p}/fd" ]] || continue
      echo "  cannot read ${p}/fd" >&2
      return 1
    fi
    stat -L -c '%d:%i' "${p}/cwd" "${p}/exe" "${p}"/fd/* 2>/dev/null || true
  done
}

sweep() {
  local root held inodes entry age unheld
  root="$(realpath -e "${SCRATCH_ROOT}")"
  held="$(mktemp)"
  inodes="$(mktemp)"
  if ! held_inodes >"${held}"; then
    rm -f "${held}" "${inodes}"
    echo "  could not read every process's open files; nothing swept" >&2
    return "${UNDETERMINED}"
  fi
  while IFS= read -r -d '' entry; do
    case "${entry##*/}" in
      systemd-private-* | snap-private-tmp | .X11-unix | .ICE-unix | .XIM-unix | .font-unix | .Test-unix) continue ;;
    esac
    age="-$((AGE_DAYS * 1440))"
    [[ -z "$(find "${entry}" -xdev \( -mmin "${age}" -o -cmin "${age}" \) -print -quit)" ]] || continue
    [[ -z "$(find "${entry}" -xdev -type s -print -quit)" ]] || continue
    unheld=0
    if find "${entry}" -xdev -printf '%D:%i\n' >"${inodes}"; then
      grep -qxFf "${held}" "${inodes}" || unheld=$?
    fi
    if [[ "${unheld}" != 1 ]]; then
      echo "  kept ${entry}: held by a running process, or could not be fully scanned"
      continue
    fi
    rm -rf --one-file-system -- "${entry}"
    echo "  removed ${entry}"
  done < <(find "${root}" -mindepth 1 -maxdepth 1 -print0)
  rm -f "${held}" "${inodes}"
}

case "${1:-}" in
  --check)
    status=0
    check || status=$?
    exit "${status}"
    ;;
  --sweep)
    status=0
    sweep || status=$?
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
  chmod 644 "$tmp"
  mv "$tmp" "${path}"
  trap - EXIT
  say "Wrote ${path}"
  [[ "${path}" == "${TMPFILES_DROPIN}" ]] || UNITS_CHANGED=1
done

if [[ "${UNITS_CHANGED}" == 1 ]]; then
  systemctl daemon-reload
fi

if [[ "$(systemctl is-enabled "${SWEEP_UNIT}.timer" 2>&1 || true)" == "enabled" ]]; then
  say "${SWEEP_UNIT}.timer already enabled, skipping"
else
  systemctl enable --now "${SWEEP_UNIT}.timer"
  say "Enabled ${SWEEP_UNIT}.timer"
fi

if [[ "$(systemctl is-enabled tmp.mount 2>&1 || true)" == "masked" ]]; then
  say "tmp.mount already masked, skipping"
else
  systemctl mask tmp.mount
  say "Masked tmp.mount"
fi

if [[ "$(findmnt -no FSTYPE -T "${SCRATCH_ROOT}" 2>/dev/null || true)" == "tmpfs" ]]; then
  cat <<EOF

NOTE: ${SCRATCH_ROOT} is still a live tmpfs. Masking tmp.mount only stops
FUTURE mounts; it moves onto disk at the next boot, or now via the operator
sequence in docs/runbook.md "Moving /tmp off RAM (agent scratch)". This
script never unmounts it.

EOF
fi
