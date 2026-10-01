#!/usr/bin/env bash
# install-host-gauges.sh — install the host-group memory gauge, the /tmp
# usage gauge and the host unit-state gauge as systemd timers (root). Same shape as the quota gauge: every 5
# minutes into node-exporter's textfile directory, run as ADMIN_USER.
# bootstrap-vps.sh calls it; on a live box run it alone to install or update
# just these timers without the rest of the bootstrap:
#
#   sudo bash /srv/lifekit-stack/scripts/host-gauge/install-host-gauges.sh

set -euo pipefail

ADMIN_USER="${ADMIN_USER:-denys}"
SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Must run as root (sudo)." >&2
  exit 1
fi

install -d -o "$ADMIN_USER" -g "$ADMIN_USER" -m 755 /var/lib/node_exporter/textfile
for gauge in host-gauge/host-group-gauge tmp-gauge/tmp-usage-gauge unit-gauge/unit-gauge; do
  name="$(basename "$gauge")"
  install -m 755 "$SCRIPTS_DIR/$gauge.sh" "/usr/local/bin/$name.sh"
  sed "s/__ADMIN_USER__/${ADMIN_USER}/" "$SCRIPTS_DIR/$gauge.service" \
    > "/etc/systemd/system/$name.service"
  install -m 644 "$SCRIPTS_DIR/$gauge.timer" "/etc/systemd/system/$name.timer"
done
systemctl daemon-reload
systemctl enable --now host-group-gauge.timer tmp-usage-gauge.timer unit-gauge.timer
