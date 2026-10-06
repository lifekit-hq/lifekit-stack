#!/usr/bin/env bash
# install-fleet-publisher.sh — install the fleet publisher as a systemd timer
# (root). Same shape as the quota gauge. FM_HOMES is the space-separated list
# of fleet homes whose state/home-summary.json is published (default FM_HOME
# alone); unset means this box has no fleet and the install is skipped.
# bootstrap-vps.sh calls it; on a live box:
#
#   sudo FM_HOMES="<fleet home> [<fleet home> ...]" bash /srv/lifekit-stack/scripts/fleet-publisher/install-fleet-publisher.sh
#
# TASKS_AXI (optional) is the absolute path of tasks-axi; unset, it is looked
# up on the admin account's PATH.
#
# Tests set INSTALL_ROOT (a prefix for every installed path) and SYSTEMCTL; an
# INSTALL_ROOT run needs no root and does not chown.

set -euo pipefail

ADMIN_USER="${ADMIN_USER:-denys}"
FM_HOMES="${FM_HOMES:-${FM_HOME:-}}"
INSTALL_ROOT="${INSTALL_ROOT:-}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

read -r -a homes <<< "$FM_HOMES"
if ((${#homes[@]} == 0)); then
  echo "FM_HOMES (or FM_HOME) is not set; skipping the fleet publisher." >&2
  exit 0
fi
# Rendered into a quoted unit Environment= value: absolute, plain characters.
for home in "${homes[@]}"; do
  [[ "$home" =~ ^/[A-Za-z0-9._/-]+$ ]] || {
    echo "fleet home $home must be an absolute path of [A-Za-z0-9._/-]" >&2
    exit 1
  }
done
FM_HOMES="${homes[*]}"
owner=(-o "$ADMIN_USER" -g "$ADMIN_USER")
if [[ -n "$INSTALL_ROOT" ]]; then
  owner=()
elif [[ $EUID -ne 0 ]]; then
  echo "Must run as root (sudo)." >&2
  exit 1
fi

# tasks-axi lives in the admin account's node toolchain, which a system unit's
# PATH does not include: resolve it as the admin account and hand the unit its
# absolute path (the publisher adds its directory, and so node, to PATH).
if [[ -z "${TASKS_AXI:-}" ]]; then
  if [[ -n "$INSTALL_ROOT" ]]; then
    TASKS_AXI="$(command -v tasks-axi || true)"
  else
    TASKS_AXI="$(su - "$ADMIN_USER" -c 'command -v tasks-axi' 2> /dev/null || true)"
  fi
fi
[[ -n "$TASKS_AXI" ]] || echo "tasks-axi not found for $ADMIN_USER; set TASKS_AXI to place boards by hold origin." >&2

UNIT_DIR="$INSTALL_ROOT/etc/systemd/system"
install -d "${owner[@]}" -m 755 "$INSTALL_ROOT/var/lib/node_exporter/textfile" "$INSTALL_ROOT/var/lib/lifekit-fleet"
install -d -m 755 "$INSTALL_ROOT/usr/local/bin" "$UNIT_DIR"
install -m 755 "$HERE/fleet-publisher.sh" "$INSTALL_ROOT/usr/local/bin/lifekit-fleet-publisher.sh"
sed -e "s|__ADMIN_USER__|${ADMIN_USER}|" -e "s|__FM_HOMES__|${FM_HOMES}|" -e "s|__TASKS_AXI__|${TASKS_AXI}|" \
  "$HERE/lifekit-fleet-publisher.service" > "$UNIT_DIR/lifekit-fleet-publisher.service"
install -m 644 "$HERE/lifekit-fleet-publisher.timer" "$UNIT_DIR/lifekit-fleet-publisher.timer"
"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable --now lifekit-fleet-publisher.timer
