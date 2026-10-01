#!/usr/bin/env bash
# Export host systemd unit state as node-exporter textfile metrics. The box's
# CI runners, tailscaled and the lifekit timers run outside Docker, so the
# container exporter cannot see them and a dead runner or a failed timer run
# was silent until a deploy or a sweep noticed. node-exporter's systemd
# collector stays off (it needs the system bus and exports every unit);
# this reads a short allowlist instead. Atomic write into the textfile
# collector directory (arg 1), same shape as
# scripts/tmp-gauge/tmp-usage-gauge.sh. Run from unit-gauge.timer (installed
# by install-host-gauges.sh) - see docs/runbook.md "Host unit gauge".
#
#   host_unit_active{unit}  1 when the unit is active, else 0. For a lifekit
#                           timer the unit is its service and active tracks
#                           the .timer (the oneshot service is inactive
#                           between runs by design).
#   host_unit_failed{unit}  1 when the unit's state is failed or its last
#                           Result is not success, else 0.
#
# A unit that is not installed on this host is skipped, not reported down. So
# is a unit that is not enabled and not running (installed but switched off on
# purpose) unless it has failed. If systemctl itself errors the script exits
# nonzero without touching host_unit.prom, so the oneshot goes failed and the
# textfile-stale rule fires instead of an empty gauge reading as healthy.
set -euo pipefail
OUT_DIR="${1:-/var/lib/node_exporter/textfile}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"

# Long-running units: a patterns list for list-unit-files, then fixed names.
RUNNER_GLOB='actions.runner.*.service'
FIXED_UNITS=(tailscaled.service docker.service containerd.service cron.service unattended-upgrades.service)
TIMER_GLOB='lifekit-*.timer'

declare -A SEEN=()
lines=()

# state UNIT -> sets LOAD, ACTIVE, RESULT, FILE_STATE (LOAD is not-found for an
# unknown unit). Exits when systemctl itself fails.
state() {
  LOAD="" ACTIVE="" RESULT="" FILE_STATE=""
  local out key value
  out="$("$SYSTEMCTL" show -p LoadState -p ActiveState -p Result -p UnitFileState "$1")" || {
    echo "unit-gauge: systemctl show $1 failed" >&2
    exit 1
  }
  while IFS='=' read -r key value; do
    case "$key" in
      LoadState) LOAD="$value" ;;
      ActiveState) ACTIVE="$value" ;;
      Result) RESULT="$value" ;;
      UnitFileState) FILE_STATE="$value" ;;
    esac
  done <<< "$out"
}

emit() { # label active_state result unit_file_state
  local active=0 failed=0
  [[ "$2" == "active" ]] && active=1
  { [[ "$2" == "failed" ]] || { [[ -n "$3" ]] && [[ "$3" != "success" ]]; }; } && failed=1
  [[ "$active" == 0 && "$failed" == 0 && "$4" != "enabled" ]] && return
  lines+=("host_unit_active{unit=\"$1\"} $active" "host_unit_failed{unit=\"$1\"} $failed")
}

# list_installed GLOB -> unit names. list-unit-files exits 1 with no output
# when nothing matches; output on a failing run is systemctl's own error.
list_installed() {
  local out rc=0
  out="$("$SYSTEMCTL" list-unit-files --no-legend --no-pager --plain "$1" 2>&1)" || rc=$?
  if [[ "$rc" != 0 && -n "$out" ]]; then
    echo "unit-gauge: systemctl list-unit-files $1 failed: $out" >&2
    exit 1
  fi
  awk 'NF {print $1}' <<< "$out"
}

units=("${FIXED_UNITS[@]}")
runners="$(list_installed "$RUNNER_GLOB")"
while IFS= read -r u; do [[ -n "$u" ]] && units+=("$u"); done <<< "$runners"
for u in "${units[@]}"; do
  [[ -n "${SEEN[$u]:-}" ]] && continue
  SEEN[$u]=1
  state "$u"
  [[ -z "$LOAD" || "$LOAD" == "not-found" ]] && continue
  emit "${u%.service}" "$ACTIVE" "$RESULT" "$FILE_STATE"
done

timers="$(list_installed "$TIMER_GLOB")"
while IFS= read -r timer; do
  [[ -z "$timer" ]] && continue
  svc="${timer%.timer}.service"
  state "$timer"
  [[ -z "$LOAD" || "$LOAD" == "not-found" ]] && continue
  timer_active="$ACTIVE" timer_file_state="$FILE_STATE"
  state "$svc"
  # A timer whose service is missing is itself broken; report it failed.
  if [[ -z "$LOAD" || "$LOAD" == "not-found" ]]; then RESULT="not-found"; fi
  emit "${svc%.service}" "$timer_active" "$RESULT" "$timer_file_state"
done <<< "$timers"

tmp="$(mktemp "$OUT_DIR/.host_unit.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
{
  echo "# HELP host_unit_active 1 when the host systemd unit (or a lifekit timer) is active."
  echo "# TYPE host_unit_active gauge"
  echo "# HELP host_unit_failed 1 when the host systemd unit is failed or its last Result is not success."
  echo "# TYPE host_unit_failed gauge"
  printf '%s\n' "${lines[@]}" | sort
} > "$tmp"
chmod 644 "$tmp"
mv -f "$tmp" "$OUT_DIR/host_unit.prom"
