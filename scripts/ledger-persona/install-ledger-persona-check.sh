#!/usr/bin/env bash
# install-ledger-persona-check.sh — install the daily Ledger persona drift
# check as a systemd timer (root). bootstrap-vps.sh calls it; on a live box:
#
#   sudo bash /srv/lifekit-stack/scripts/ledger-persona/install-ledger-persona-check.sh
#
# The unit runs drift-alert.py from the checkout (REPO_DIR, default the repo
# this script sits in) as LIFEKIT_USER. Undo: docs/runbook.md "Ledger persona
# drift alert". Tests set INSTALL_ROOT (a prefix for every installed path) and
# SYSTEMCTL; an INSTALL_ROOT run needs no root.

set -euo pipefail

LIFEKIT_USER="${LIFEKIT_USER:-lifekit}"
INSTALL_ROOT="${INSTALL_ROOT:-}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "$HERE/../.." && pwd)}"

if [[ -z "$INSTALL_ROOT" && $EUID -ne 0 ]]; then
  echo "Must run as root (sudo)." >&2
  exit 1
fi

UNIT_DIR="$INSTALL_ROOT/etc/systemd/system"
install -d -m 755 "$UNIT_DIR"
sed -e "s|__LIFEKIT_USER__|${LIFEKIT_USER}|g" -e "s|__REPO_DIR__|${REPO_DIR}|g" \
  "$HERE/lifekit-ledger-persona-check.service" > "$UNIT_DIR/lifekit-ledger-persona-check.service"
install -m 644 "$HERE/lifekit-ledger-persona-check.timer" "$UNIT_DIR/lifekit-ledger-persona-check.timer"
"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable --now lifekit-ledger-persona-check.timer
