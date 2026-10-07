#!/usr/bin/env bash
# install-docker-weekly-prune.sh — install the weekly Docker prune as a
# systemd timer (root). Not called by bootstrap-vps.sh: it is installed only
# by an explicit operator step:
#
#   sudo bash /srv/lifekit-stack/scripts/docker-weekly-prune/install-docker-weekly-prune.sh
#
# Tests set INSTALL_ROOT (a prefix for every installed path) and SYSTEMCTL; an
# INSTALL_ROOT run needs no root.

set -euo pipefail

INSTALL_ROOT="${INSTALL_ROOT:-}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -z "$INSTALL_ROOT" && $EUID -ne 0 ]]; then
  echo "Must run as root (sudo)." >&2
  exit 1
fi

UNIT_DIR="$INSTALL_ROOT/etc/systemd/system"
install -d -m 755 "$INSTALL_ROOT/usr/local/bin" "$UNIT_DIR"
install -m 755 "$HERE/docker-weekly-prune.sh" "$INSTALL_ROOT/usr/local/bin/lifekit-docker-weekly-prune.sh"
install -m 644 "$HERE/lifekit-docker-weekly-prune.service" "$UNIT_DIR/lifekit-docker-weekly-prune.service"
install -m 644 "$HERE/lifekit-docker-weekly-prune.timer" "$UNIT_DIR/lifekit-docker-weekly-prune.timer"
"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable --now lifekit-docker-weekly-prune.timer
