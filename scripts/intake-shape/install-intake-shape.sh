#!/usr/bin/env bash
# install-intake-shape.sh - install the timer that gives every accepted intake
# idea a shaping item and files the ready issue on the owner's answer (root):
#
#   sudo INTAKE_SHAPE_FM_HOME=/home/denys/fm-vps \
#     INTAKE_SHAPE_REPOS="lifekit-stack=lifekit-hq/lifekit-stack" \
#     bash /srv/lifekit-stack/scripts/intake-shape/install-intake-shape.sh
#
# Undo is in docs/runbook.md "Intake shape step". Tests set INSTALL_ROOT (a
# prefix for every installed path), SYSTEMCTL and ADMIN_USER; an INSTALL_ROOT
# run needs no root.

set -euo pipefail

INSTALL_ROOT="${INSTALL_ROOT:-}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
ADMIN_USER="${ADMIN_USER:-denys}"
FM_HOME="${INTAKE_SHAPE_FM_HOME:-}"
REPOS="${INTAKE_SHAPE_REPOS:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -z "$INSTALL_ROOT" && $EUID -ne 0 ]]; then
  echo "Must run as root (sudo)." >&2
  exit 1
fi
[[ "$FM_HOME" =~ ^/[A-Za-z0-9._/-]+$ ]] || {
  echo "INTAKE_SHAPE_FM_HOME must be the firstmate home, an absolute path of [A-Za-z0-9._/-]" >&2
  exit 1
}
[[ "$REPOS" =~ ^[A-Za-z0-9._/=\ -]+$ ]] || {
  echo "INTAKE_SHAPE_REPOS must be space-separated <project>=<owner/repo> pairs" >&2
  exit 1
}

UNIT_DIR="$INSTALL_ROOT/etc/systemd/system"
BIN_DIR="$INSTALL_ROOT/usr/local/libexec/lifekit-intake-shape"
install -d -m 755 "$BIN_DIR" "$UNIT_DIR"
install -m 755 "$HERE/intake_shape.py" "$BIN_DIR/intake_shape.py"
sed -e "s|__ADMIN_USER__|${ADMIN_USER}|" -e "s|__FM_HOME__|${FM_HOME}|" \
  -e "s|__REPOS__|${REPOS}|" "$HERE/lifekit-intake-shape.service" \
  > "$UNIT_DIR/lifekit-intake-shape.service"
chmod 644 "$UNIT_DIR/lifekit-intake-shape.service"
install -m 644 "$HERE/lifekit-intake-shape.timer" "$UNIT_DIR/lifekit-intake-shape.timer"
"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable --now lifekit-intake-shape.timer
echo "intake shape step installed for $FM_HOME"
