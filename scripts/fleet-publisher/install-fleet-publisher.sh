#!/usr/bin/env bash
# install-fleet-publisher.sh — install the fleet publisher as a systemd timer
# (root). Same shape as the quota gauge. FM_HOME is the fleet home whose
# state/home-summary.json is published; unset means this box has no fleet
# and the install is skipped. bootstrap-vps.sh calls it; on a live box:
#
#   sudo FM_HOME=<fleet home> bash /srv/lifekit-stack/scripts/fleet-publisher/install-fleet-publisher.sh

set -euo pipefail

ADMIN_USER="${ADMIN_USER:-denys}"
FM_HOME="${FM_HOME:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -z "$FM_HOME" ]]; then
  echo "FM_HOME is not set; skipping the fleet publisher." >&2
  exit 0
fi
if [[ $EUID -ne 0 ]]; then
  echo "Must run as root (sudo)." >&2
  exit 1
fi

install -d -o "$ADMIN_USER" -g "$ADMIN_USER" -m 755 /var/lib/node_exporter/textfile /var/lib/lifekit-fleet
install -m 755 "$HERE/fleet-publisher.sh" /usr/local/bin/lifekit-fleet-publisher.sh
sed -e "s|__ADMIN_USER__|${ADMIN_USER}|" -e "s|__FM_HOME__|${FM_HOME}|" \
  "$HERE/lifekit-fleet-publisher.service" > /etc/systemd/system/lifekit-fleet-publisher.service
install -m 644 "$HERE/lifekit-fleet-publisher.timer" /etc/systemd/system/lifekit-fleet-publisher.timer
systemctl daemon-reload
systemctl enable --now lifekit-fleet-publisher.timer
