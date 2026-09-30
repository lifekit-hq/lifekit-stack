#!/usr/bin/env bash
# install-config-sync.sh — install the openclaw-config sync script and its
# 15-minute timer (root). bootstrap-vps.sh calls it; on a live box run it alone
# to refresh /usr/local/bin and the units from the repo:
#
#   sudo bash /srv/lifekit-stack/scripts/sync/install-config-sync.sh

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Must run as root (sudo)." >&2
  exit 1
fi

install -m 755 "$HERE/openclaw-config-sync.sh" /usr/local/bin/openclaw-config-sync.sh
install -m 644 "$HERE/openclaw-config-sync.service" /etc/systemd/system/openclaw-config-sync.service
install -m 644 "$HERE/openclaw-config-sync.timer" /etc/systemd/system/openclaw-config-sync.timer
systemctl daemon-reload
systemctl enable --now openclaw-config-sync.timer
