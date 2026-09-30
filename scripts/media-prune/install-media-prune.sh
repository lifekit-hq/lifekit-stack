#!/usr/bin/env bash
# install-media-prune.sh — install the daily inbound-media prune as a systemd
# timer (root). bootstrap-vps.sh calls it; on a live box run it alone:
#
#   sudo bash /srv/lifekit-stack/scripts/media-prune/install-media-prune.sh

set -euo pipefail

LIFEKIT_USER="${LIFEKIT_USER:-lifekit}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Must run as root (sudo)." >&2
  exit 1
fi

install -m 755 "$HERE/media-prune.sh" /usr/local/bin/media-prune.sh
sed "s/__LIFEKIT_USER__/${LIFEKIT_USER}/g" "$HERE/media-prune.service" \
  > /etc/systemd/system/media-prune.service
install -m 644 "$HERE/media-prune.timer" /etc/systemd/system/media-prune.timer
systemctl daemon-reload
systemctl enable --now media-prune.timer
