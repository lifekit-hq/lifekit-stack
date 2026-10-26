#!/usr/bin/env bash
# install-intake-to-vault.sh - install the hourly timer that files each accepted
# video-eval report into the memory vault as a source page, its index line and
# one log line (root). bootstrap-vps.sh calls it; on a live box:
#
#   sudo bash /srv/lifekit-stack/scripts/intake-to-vault/install-intake-to-vault.sh
#
# The unit skips while there is no vault clone; undo is in docs/runbook.md
# "Intake to vault".
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
BIN_DIR="$INSTALL_ROOT/usr/local/libexec/lifekit-intake-to-vault"
install -d -m 755 "$BIN_DIR" "$UNIT_DIR"
install -m 755 "$HERE/intake_to_vault.py" "$BIN_DIR/intake_to_vault.py"
install -m 644 "$HERE/lifekit-intake-to-vault.service" "$UNIT_DIR/lifekit-intake-to-vault.service"
install -m 644 "$HERE/lifekit-intake-to-vault.timer" "$UNIT_DIR/lifekit-intake-to-vault.timer"
"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable --now lifekit-intake-to-vault.timer
