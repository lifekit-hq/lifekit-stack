#!/usr/bin/env bash
# install-intake-shape.sh - install the timer that gives every accepted intake
# idea a shaping item and files the ready issue on the owner's answer (root):
#
#   sudo INTAKE_SHAPE_FM_HOME=/home/denys/fm-vps \
#     INTAKE_SHAPE_REPOS="lifekit-stack=lifekit-hq/lifekit-stack" \
#     bash /srv/lifekit-stack/scripts/intake-shape/install-intake-shape.sh
#
# TASKS_AXI (optional) is the absolute path of tasks-axi; unset, it is looked up
# as the admin account (login shell, then an interactive one for a node version
# manager set up in .bashrc). The unit gets TASKS_AXI= and a PATH that starts
# with that tool's directory (so fm-captain-hold.sh finds tasks-axi and node);
# no tasks-axi, no install.
#
# Undo is in docs/runbook.md "Intake shape step". Tests set INSTALL_ROOT (a
# prefix for every installed path), SYSTEMCTL, SU and ADMIN_USER; an INSTALL_ROOT
# run needs no root.

set -euo pipefail

INSTALL_ROOT="${INSTALL_ROOT:-}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
SU="${SU:-su}"
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

# A system unit's PATH has no node toolchain: resolve tasks-axi as the admin
# account, the same lookup as the fleet publisher installer.
if [[ -z "${TASKS_AXI:-}" ]]; then
  if [[ -n "$INSTALL_ROOT" && "$SU" == su ]]; then
    TASKS_AXI="$(command -v tasks-axi || true)"
  else
    TASKS_AXI="$("$SU" - "$ADMIN_USER" -c 'command -v tasks-axi' 2> /dev/null || true)"
    [[ "$TASKS_AXI" == /* ]] ||
      TASKS_AXI="$("$SU" - "$ADMIN_USER" -c 'bash -ic "command -v tasks-axi"' 2> /dev/null | tail -n 1 || true)"
  fi
fi
[[ "$TASKS_AXI" =~ ^/[A-Za-z0-9._/-]+$ ]] || {
  echo "tasks-axi not found for $ADMIN_USER; set TASKS_AXI to its absolute path of [A-Za-z0-9._/-]" >&2
  exit 1
}
TASKS_AXI_DIR="${TASKS_AXI%/*}"
UNIT_PATH="${TASKS_AXI_DIR:-/}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

UNIT_DIR="$INSTALL_ROOT/etc/systemd/system"
BIN_DIR="$INSTALL_ROOT/usr/local/libexec/lifekit-intake-shape"
install -d -m 755 "$BIN_DIR" "$UNIT_DIR"
install -m 755 "$HERE/intake_shape.py" "$BIN_DIR/intake_shape.py"
sed -e "s|__ADMIN_USER__|${ADMIN_USER}|g" -e "s|__FM_HOME__|${FM_HOME}|" \
  -e "s|__REPOS__|${REPOS}|" -e "s|__TASKS_AXI__|${TASKS_AXI}|" -e "s|__UNIT_PATH__|${UNIT_PATH}|" "$HERE/lifekit-intake-shape.service" \
  > "$UNIT_DIR/lifekit-intake-shape.service"
chmod 644 "$UNIT_DIR/lifekit-intake-shape.service"
install -m 644 "$HERE/lifekit-intake-shape.timer" "$UNIT_DIR/lifekit-intake-shape.timer"
"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable --now lifekit-intake-shape.timer
echo "intake shape step installed for $FM_HOME"

# Record the installed commit (issue 689; the release gauge reads it).
bash "$(dirname "${BASH_SOURCE[0]}")/../lib/install-stamp.sh" intake-shape
