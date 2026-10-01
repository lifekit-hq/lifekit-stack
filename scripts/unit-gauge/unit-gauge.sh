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
# A unit that is not installed on this host is skipped, not reported down.
set -euo pipefail
OUT_DIR="${1:-/var/lib/node_exporter/textfile}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"

# Long-running units: a patterns list for list-unit-files, then fixed names.
RUNNER_GLOB='actions.runner.*.service'
FIXED_UNITS=(tailscaled.service docker.service containerd.service cron.service unattended-upgrades.service)
TIMER_GLOB='lifekit-*.timer'

declare -A SEEN=()
lines=()

# state UNIT -> sets LOAD, ACTIVE, RESULT (empty when the unit is unknown).
state() {
  LOAD="" ACTIVE="" RESULT=""
  local key value
  while IFS='=' read -r key value; do
    case "$key" in
      LoadState) LOAD="$value" ;;
      ActiveState) ACTIVE="$value" ;;
      Result) RESULT="$value" ;;
    esac
  done < <("$SYSTEMCTL" show -p LoadState -p ActiveState -p Result "$1" 2>/dev/null || true)
}

emit() { # label active_state result
  local active=0 failed=0
  [[ "$2" == "active" ]] && active=1
  { [[ "$2" == "failed" ]] || { [[ -n "$3" ]] && [[ "$3" != "success" ]]; }; } && failed=1
  lines+=("host_unit_active{unit=\"$1\"} $active" "host_unit_failed{unit=\"$1\"} $failed")
}

list_installed() { # glob -> unit names
  "$SYSTEMCTL" list-unit-files --no-legend --no-pager --plain "$1" 2>/dev/null | awk 'NF {print $1}' || true
}

units=("${FIXED_UNITS[@]}")
while IFS= read -r u; do [[ -n "$u" ]] && units+=("$u"); done < <(list_installed "$RUNNER_GLOB")
for u in "${units[@]}"; do
  [[ -n "${SEEN[$u]:-}" ]] && continue
  SEEN[$u]=1
  state "$u"
  [[ -z "$LOAD" || "$LOAD" == "not-found" ]] && continue
  emit "${u%.service}" "$ACTIVE" "$RESULT"
done

while IFS= read -r timer; do
  [[ -z "$timer" ]] && continue
  svc="${timer%.timer}.service"
  state "$timer"
  [[ -z "$LOAD" || "$LOAD" == "not-found" ]] && continue
  timer_active="$ACTIVE"
  state "$svc"
  # A timer whose service is missing is itself broken; report it failed.
  if [[ -z "$LOAD" || "$LOAD" == "not-found" ]]; then RESULT="not-found"; fi
  emit "${svc%.service}" "$timer_active" "$RESULT"
done < <(list_installed "$TIMER_GLOB")

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
