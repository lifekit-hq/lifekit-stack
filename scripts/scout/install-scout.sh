#!/usr/bin/env bash
# install-scout.sh - install the scout fetch: the Debian feed2exec package and
# the systemd service + timer. It does NOT enable or start the timer: the
# pipeline is switched on only after its measured firing (design report,
# section 8). To enable later: systemctl enable --now lifekit-scout.timer
#
#   sudo FM_INBOX_BIN=/path/to/fm-inbox.sh FM_INBOX_HOME=/path/to/target/home \
#     bash /srv/lifekit-stack/scripts/scout/install-scout.sh
#
# FM_INBOX_BIN / FM_INBOX_HOME (and SCOUT_LENS_DIR when set) are written to
# /etc/lifekit/scout.env, the unit's EnvironmentFile. The unit runs as
# SCOUT_USER (default ADMIN_USER, then denys).
#
# Undo: apt-get remove feed2exec; remove the two units and the env file; delete
# ~/.local/state/lifekit-scout.
#
# Tests set INSTALL_ROOT (a prefix for every installed path), SYSTEMCTL and
# APT_GET; an INSTALL_ROOT run needs no root.

set -euo pipefail

INSTALL_ROOT="${INSTALL_ROOT:-}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
APT_GET="${APT_GET:-apt-get}"
ADMIN_USER="${ADMIN_USER:-denys}"
SCOUT_USER="${SCOUT_USER:-$ADMIN_USER}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "$HERE/../.." && pwd)}"

if [[ -z "$INSTALL_ROOT" && $EUID -ne 0 ]]; then
  echo "Must run as root (sudo)." >&2
  exit 1
fi

"$APT_GET" install -y feed2exec

ENV_DIR="$INSTALL_ROOT/etc/lifekit"
ENV_FILE="$ENV_DIR/scout.env"
if [[ -n "${FM_INBOX_BIN:-}" ]]; then
  install -d -m 755 "$ENV_DIR"
  {
    echo "FM_INBOX_BIN=$FM_INBOX_BIN"
    [[ -n "${FM_INBOX_HOME:-}" ]] && echo "FM_INBOX_HOME=$FM_INBOX_HOME"
    [[ -n "${SCOUT_LENS_DIR:-}" ]] && echo "SCOUT_LENS_DIR=$SCOUT_LENS_DIR"
  } > "$ENV_FILE"
  chmod 644 "$ENV_FILE"
elif [[ ! -f "$ENV_FILE" ]]; then
  echo "scout: FM_INBOX_BIN not set and no $ENV_FILE yet; the units are installed but the run will fail until it exists." >&2
fi

UNIT_DIR="$INSTALL_ROOT/etc/systemd/system"
install -d -m 755 "$UNIT_DIR"
sed -e "s|__SCOUT_USER__|${SCOUT_USER}|g" -e "s|__REPO_DIR__|${REPO_DIR}|g" \
  "$HERE/lifekit-scout.service" > "$UNIT_DIR/lifekit-scout.service"
install -m 644 "$HERE/lifekit-scout.timer" "$UNIT_DIR/lifekit-scout.timer"
"$SYSTEMCTL" daemon-reload
echo "scout: installed; timer NOT enabled (systemctl enable --now lifekit-scout.timer)"
