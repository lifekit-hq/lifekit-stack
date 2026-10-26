#!/usr/bin/env bash
# install-vault-audit.sh - install the weekly vault-audit timer (root).
# bootstrap-vps.sh calls it; on a live box:
#
#   sudo bash /srv/lifekit-stack/scripts/memory-audit/install-vault-audit.sh
#
# Only the two units are installed: the service runs the runner from the
# checkout, so deploy.sh keeps it current. The unit skips while there is no
# vault clone; undo is in docs/runbook.md "Weekly vault audit (host timer)".
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
install -d -m 755 "$UNIT_DIR"
install -m 644 "$HERE/lifekit-vault-audit.service" "$UNIT_DIR/lifekit-vault-audit.service"
install -m 644 "$HERE/lifekit-vault-audit.timer" "$UNIT_DIR/lifekit-vault-audit.timer"
"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable --now lifekit-vault-audit.timer
