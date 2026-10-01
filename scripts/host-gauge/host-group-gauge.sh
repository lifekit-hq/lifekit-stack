#!/usr/bin/env bash
# Export RAM and swap for the host groups that are not containers as
# node-exporter textfile metrics, so the operator sessions, CI runners and OS
# rows of docs/resource-budget.md are measurable. Reads cgroup v2 only
# (memory.current, memory.stat, memory.swap.current) and writes atomically into
# the textfile collector directory (arg 1), the shape of
# scripts/quota-gauge/claude-quota-gauge.sh. Runs from host-group-gauge.timer.
#
# Groups (CGROUP_ROOT overrides /sys/fs/cgroup; tests point it at a fixture):
#   operator  the operator's login slice, HOST_GAUGE_OPERATOR_SLICE
#             (default user.slice/user-1001.slice)
#   runners   system.slice/actions.runner.*.service
#   os        the daemon units in HOST_GAUGE_OS_UNITS (default docker,
#             containerd, tailscaled, systemd-journald) under system.slice.
#             Never all of system.slice: docker's container scopes live there.
# RAM is memory.current minus inactive_file - resident memory without
# reclaimable page cache, the budget's own definition. A group with no cgroup
# present reports 0. Also writes host_vmstat_pswpin_pages_total (pages swapped
# in since boot, from VMSTAT_FILE, default /proc/vmstat) - node-exporter's vmstat
# collector is disabled, and the host-memory-pressure alert reads its rate as the
# sustained swap-in signal. Read-only on the host; a failed run leaves the old file.
set -euo pipefail
OUT_DIR="${1:-/var/lib/node_exporter/textfile}"
ROOT="${CGROUP_ROOT:-/sys/fs/cgroup}"
OPERATOR_SLICE="${HOST_GAUGE_OPERATOR_SLICE:-user.slice/user-1001.slice}"
VMSTAT_FILE="${VMSTAT_FILE:-/proc/vmstat}"
OS_UNITS="${HOST_GAUGE_OS_UNITS:-docker containerd tailscaled systemd-journald}"

# Prints "<ram_bytes> <swap_bytes>" summed over the cgroup directories given.
sum_groups() {
  local ram=0 swap=0 d cur inactive sw
  for d in "$@"; do
    [[ -r "$d/memory.current" ]] || continue
    cur="$(<"$d/memory.current")"
    inactive="$(awk '$1 == "inactive_file" {print $2}' "$d/memory.stat" 2>/dev/null || true)"
    ram=$((ram + cur - ${inactive:-0}))
    sw=0
    [[ -r "$d/memory.swap.current" ]] && sw="$(<"$d/memory.swap.current")"
    swap=$((swap + sw))
  done
  echo "$ram $swap"
}

os_dirs=()
for u in $OS_UNITS; do os_dirs+=("$ROOT/system.slice/$u.service"); done
shopt -s nullglob
runner_dirs=("$ROOT"/system.slice/actions.runner.*.service)
shopt -u nullglob

tmp="$(mktemp "$OUT_DIR/.host_group.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
{
  echo "# HELP host_group_memory_bytes Resident memory of a host group, page cache excluded."
  echo "# TYPE host_group_memory_bytes gauge"
  echo "# HELP host_group_memory_swap_bytes Swapped-out memory of a host group."
  echo "# TYPE host_group_memory_swap_bytes gauge"
  for g in operator runners os; do
    case "$g" in
      operator) read -r ram swap < <(sum_groups "$ROOT/$OPERATOR_SLICE") ;;
      runners) read -r ram swap < <(sum_groups "${runner_dirs[@]}") ;;
      os) read -r ram swap < <(sum_groups "${os_dirs[@]}") ;;
    esac
    echo "host_group_memory_bytes{group=\"$g\"} $ram"
    echo "host_group_memory_swap_bytes{group=\"$g\"} $swap"
  done
  echo "# HELP host_vmstat_pswpin_pages_total Pages swapped in since boot."
  echo "# TYPE host_vmstat_pswpin_pages_total counter"
  echo "host_vmstat_pswpin_pages_total $(awk '$1 == "pswpin" {print $2}' "$VMSTAT_FILE" 2>/dev/null | head -n1 | grep . || echo 0)"
} > "$tmp"
chmod 644 "$tmp"
mv -f "$tmp" "$OUT_DIR/host_group.prom"
