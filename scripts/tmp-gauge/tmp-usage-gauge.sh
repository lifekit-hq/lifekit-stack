#!/usr/bin/env bash
# Export /tmp usage as node-exporter textfile metrics. /tmp on lifekit-vps is
# a 7.8 GB tmpfs (RAM-backed), and node-exporter's own filesystem collector
# excludes tmpfs (compose/docker-compose.yml node-exporter
# fs-types-exclude), so nothing else on the box reports it - the gap that
# let /tmp fill to 100% unnoticed on 2026-09-29 and cost every Claude
# session its tool output. Atomic write into the textfile collector
# directory (arg 1), same shape as
# scripts/quota-gauge/claude-quota-gauge.sh. Run from tmp-usage-gauge.timer
# (installed by install-host-gauges.sh) - see docs/runbook.md.
set -euo pipefail
OUT_DIR="${1:-/var/lib/node_exporter/textfile}"
read -r size avail < <(df -B1 --output=size,avail /tmp | tail -n1)
tmp="$(mktemp "$OUT_DIR/.tmp_usage.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
{
  echo "tmp_filesystem_size_bytes $size"
  echo "tmp_filesystem_avail_bytes $avail"
} > "$tmp"
chmod 644 "$tmp"
mv -f "$tmp" "$OUT_DIR/tmp_usage.prom"
