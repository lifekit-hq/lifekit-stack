#!/usr/bin/env bash
# install-vault-door.sh - install the vault door: a systemd timer that forwards
# ChatGPT's vault inbox notes to the main firstmate captain inbox (root). FM_HOME is the main
# firstmate home that receives the notes; unset means this box has no fleet and
# the install is skipped. bootstrap-vps.sh calls it; on a live box:
#
#   sudo FM_HOME=<main fleet home> bash /srv/lifekit-stack/scripts/vault-door/install-vault-door.sh
#
# The units do nothing until a ChatGPT note appears; undo is in docs/runbook.md
# "Vault door".
#
# Tests set INSTALL_ROOT (a prefix for every installed path) and SYSTEMCTL; an
# INSTALL_ROOT run needs no root.

set -euo pipefail

ADMIN_USER="${ADMIN_USER:-denys}"
FM_HOME="${FM_HOME:-}"
INSTALL_ROOT="${INSTALL_ROOT:-}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -z "$FM_HOME" ]]; then
  echo "FM_HOME is not set; skipping the vault door." >&2
  exit 0
fi
# Rendered into a quoted unit Environment= value: absolute, plain characters.
[[ "$FM_HOME" =~ ^/[A-Za-z0-9._/-]+$ ]] || {
  echo "FM_HOME $FM_HOME must be an absolute path of [A-Za-z0-9._/-]" >&2
  exit 1
}
if [[ -z "$INSTALL_ROOT" && $EUID -ne 0 ]]; then
  echo "Must run as root (sudo)." >&2
  exit 1
fi

UNIT_DIR="$INSTALL_ROOT/etc/systemd/system"
BIN_DIR="$INSTALL_ROOT/usr/local/libexec/lifekit-vault-door"
install -d -m 755 "$BIN_DIR" "$UNIT_DIR"
install -m 755 "$HERE/vault_door.py" "$BIN_DIR/vault_door.py"
sed -e "s|__ADMIN_USER__|${ADMIN_USER}|" -e "s|__FM_HOME__|${FM_HOME}|" \
  "$HERE/lifekit-vault-door-bridge.service" > "$UNIT_DIR/lifekit-vault-door-bridge.service"
chmod 644 "$UNIT_DIR/lifekit-vault-door-bridge.service"
install -m 644 "$HERE/lifekit-vault-door-bridge.timer" "$UNIT_DIR/lifekit-vault-door-bridge.timer"
"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable --now lifekit-vault-door-bridge.timer
