#!/usr/bin/env bash
# install-offbox-backup.sh — install the nightly off-box backup and its
# monthly restore check as systemd timers (root). bootstrap-vps.sh calls it;
# on a live box:
#
#   sudo bash /srv/lifekit-stack/scripts/offbox-backup/install-offbox-backup.sh
#
# Installs age and zstd when missing and a pinned upstream rclone (below),
# writes both units with the deploy account (LIFEKIT_USER), the checkout
# (REPO_DIR, default the repo this script sits in) and that rclone, and
# enables both timers. Until the R2 values are in the
# env file each run logs one "not configured" line and exits 0, so enabling
# early is safe (docs/runbook.md "Off-box backup (R2)"). Tests set
# INSTALL_ROOT (a prefix for every installed path, no packages, no root),
# SYSTEMCTL, and RCLONE_BASE_URL / RCLONE_SHA256 / RCLONE_ARCH for the
# download.

set -euo pipefail

LIFEKIT_USER="${LIFEKIT_USER:-lifekit}"
INSTALL_ROOT="${INSTALL_ROOT:-}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "$HERE/../.." && pwd)}"
UNITS=(lifekit-offbox-backup lifekit-offbox-verify)

# rclone comes from rclone.org, not Debian: trixie's 1.60.1 HEADs every
# upload by versionId, which R2 answers with 501 Not Implemented, so each
# rcat fails after the object is written. To bump: the version and both
# zips' sums from <RCLONE_BASE_URL>/<version>/SHA256SUMS.
RCLONE_VERSION="v1.75.1"
RCLONE_BASE_URL="${RCLONE_BASE_URL:-https://downloads.rclone.org}"
RCLONE_ARCH="${RCLONE_ARCH:-$(dpkg --print-architecture)}"
case "$RCLONE_ARCH" in
  arm64) sum="03f2504174034b6d004152ed7369251c9a9ec1f7e0836eda420f5c7a5ec0dff9" ;;
  amd64) sum="982b5aa772841168f8e380f139e9e787b2a105403e32b94da8676a0e1c0a13ab" ;;
  *)
    echo "no pinned rclone for architecture $RCLONE_ARCH" >&2
    exit 1
    ;;
esac
RCLONE_SHA256="${RCLONE_SHA256:-$sum}"
RCLONE_BIN="/usr/local/lib/lifekit-offbox/rclone-$RCLONE_VERSION"

if [[ -z "$INSTALL_ROOT" && $EUID -ne 0 ]]; then
  echo "Must run as root (sudo)." >&2
  exit 1
fi

if [[ -z "$INSTALL_ROOT" ]]; then
  missing=()
  for pkg in age zstd; do
    command -v "$pkg" >/dev/null || missing+=("$pkg")
  done
  if ((${#missing[@]})); then
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
  fi
fi

if [[ -x "$INSTALL_ROOT$RCLONE_BIN" ]]; then
  echo "rclone $RCLONE_VERSION already in place"
else
  zip="rclone-$RCLONE_VERSION-linux-$RCLONE_ARCH.zip"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  curl -fsSL --retry 3 -o "$tmp/$zip" "$RCLONE_BASE_URL/$RCLONE_VERSION/$zip"
  if ! echo "$RCLONE_SHA256  $tmp/$zip" | sha256sum -c - >/dev/null 2>&1; then
    echo "$zip does not match its pinned sha256; nothing installed" >&2
    exit 1
  fi
  python3 -I -c 'import sys, zipfile
open(sys.argv[3], "wb").write(zipfile.ZipFile(sys.argv[1]).read(sys.argv[2]))' \
    "$tmp/$zip" "${zip%.zip}/rclone" "$tmp/rclone"
  install -D -m 755 "$tmp/rclone" "$INSTALL_ROOT$RCLONE_BIN"
  echo "rclone $RCLONE_VERSION installed"
fi

UNIT_DIR="$INSTALL_ROOT/etc/systemd/system"
install -d -m 755 "$UNIT_DIR"
for unit in "${UNITS[@]}"; do
  sed -e "s|__LIFEKIT_USER__|${LIFEKIT_USER}|g" -e "s|__REPO_DIR__|${REPO_DIR}|g" \
    -e "s|__RCLONE_BIN__|${RCLONE_BIN}|g" \
    "$HERE/$unit.service" > "$UNIT_DIR/$unit.service"
  install -m 644 "$HERE/$unit.timer" "$UNIT_DIR/$unit.timer"
done
"$SYSTEMCTL" daemon-reload
for unit in "${UNITS[@]}"; do
  "$SYSTEMCTL" enable --now "$unit.timer"
done
