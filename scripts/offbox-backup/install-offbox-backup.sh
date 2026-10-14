#!/usr/bin/env bash
# install-offbox-backup.sh — install the nightly off-box backup and its
# monthly restore check as systemd timers (root). bootstrap-vps.sh calls it;
# on a live box:
#
#   sudo bash /srv/lifekit-stack/scripts/offbox-backup/install-offbox-backup.sh
#
# Installs age and rclone when missing, writes both units with the deploy
# account (LIFEKIT_USER) and the checkout (REPO_DIR, default the repo this
# script sits in), and enables both timers. Until the R2 values are in the
# env file each run logs one "not configured" line and exits 0, so enabling
# early is safe (docs/runbook.md "Off-box backup (R2)"). Tests set
# INSTALL_ROOT (a prefix for every installed path, no packages, no root) and
# SYSTEMCTL.

set -euo pipefail

LIFEKIT_USER="${LIFEKIT_USER:-lifekit}"
INSTALL_ROOT="${INSTALL_ROOT:-}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "$HERE/../.." && pwd)}"
UNITS=(lifekit-offbox-backup lifekit-offbox-verify)

if [[ -z "$INSTALL_ROOT" && $EUID -ne 0 ]]; then
  echo "Must run as root (sudo)." >&2
  exit 1
fi

if [[ -z "$INSTALL_ROOT" ]]; then
  missing=()
  for pkg in age rclone zstd; do
    command -v "$pkg" >/dev/null || missing+=("$pkg")
  done
  if ((${#missing[@]})); then
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
  fi
fi

UNIT_DIR="$INSTALL_ROOT/etc/systemd/system"
install -d -m 755 "$UNIT_DIR"
for unit in "${UNITS[@]}"; do
  sed -e "s|__LIFEKIT_USER__|${LIFEKIT_USER}|g" -e "s|__REPO_DIR__|${REPO_DIR}|g" \
    "$HERE/$unit.service" > "$UNIT_DIR/$unit.service"
  install -m 644 "$HERE/$unit.timer" "$UNIT_DIR/$unit.timer"
done
"$SYSTEMCTL" daemon-reload
for unit in "${UNITS[@]}"; do
  "$SYSTEMCTL" enable --now "$unit.timer"
done
