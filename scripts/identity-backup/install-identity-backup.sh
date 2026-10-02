#!/usr/bin/env bash
# install-identity-backup.sh — install the nightly identity database dump as
# a systemd timer (root). bootstrap-vps.sh calls it; on a live box:
#
#   sudo bash /srv/lifekit-stack/scripts/identity-backup/install-identity-backup.sh
#
# Tests set INSTALL_ROOT (a prefix for every installed path) and SYSTEMCTL; an
# INSTALL_ROOT run needs no root.

set -euo pipefail

LIFEKIT_USER="${LIFEKIT_USER:-lifekit}"
INSTALL_ROOT="${INSTALL_ROOT:-}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -z "$INSTALL_ROOT" && $EUID -ne 0 ]]; then
  echo "Must run as root (sudo)." >&2
  exit 1
fi

UNIT_DIR="$INSTALL_ROOT/etc/systemd/system"
install -d -m 755 "$INSTALL_ROOT/usr/local/bin" "$UNIT_DIR"
install -m 755 "$HERE/identity-backup.sh" "$INSTALL_ROOT/usr/local/bin/lifekit-identity-backup.sh"
sed "s/__LIFEKIT_USER__/${LIFEKIT_USER}/g" "$HERE/lifekit-identity-backup.service" \
  > "$UNIT_DIR/lifekit-identity-backup.service"
install -m 644 "$HERE/lifekit-identity-backup.timer" "$UNIT_DIR/lifekit-identity-backup.timer"
"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable --now lifekit-identity-backup.timer
