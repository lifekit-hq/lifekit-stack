#!/usr/bin/env bash
# install-config-sync.sh — install the openclaw-config sync script and its
# 15-minute timer (root). bootstrap-vps.sh calls it; on a live box run it alone
# to refresh /usr/local/bin and the units from the repo:
#
#   sudo bash /srv/lifekit-stack/scripts/sync/install-config-sync.sh
#
# deploy.sh only reports when the installed copy drifts from the repo
# (`--check`, report-only; the deploy account has no sudo).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${CONFIG_SYNC_UNIT_DIR:-/etc/systemd/system}"
BIN="${CONFIG_SYNC_BIN:-/usr/local/bin/openclaw-config-sync.sh}"

if [[ "${1:-}" == "--check" ]]; then
  mismatch=0
  for pair in "$HERE/openclaw-config-sync.sh:$BIN" \
    "$HERE/openclaw-config-sync.service:$UNIT_DIR/openclaw-config-sync.service" \
    "$HERE/openclaw-config-sync.timer:$UNIT_DIR/openclaw-config-sync.timer"; do
    src="${pair%%:*}" dst="${pair#*:}"
    if cmp -s "$src" "$dst"; then
      echo "  $dst: matches"
    elif [[ -f "$dst" ]]; then
      echo "  $dst: present but does not match the repository content"
      mismatch=1
    else
      echo "  $dst: absent"
      mismatch=1
    fi
  done
  state="$(systemctl is-enabled openclaw-config-sync.timer 2>&1 || true)"
  echo "  openclaw-config-sync.timer: $state"
  [[ "$state" == "enabled" ]] || mismatch=1
  exit "$mismatch"
fi

if [[ $EUID -ne 0 ]]; then
  echo "Must run as root (sudo)." >&2
  exit 1
fi

install -m 755 "$HERE/openclaw-config-sync.sh" "$BIN"
install -m 644 "$HERE/openclaw-config-sync.service" "$UNIT_DIR/openclaw-config-sync.service"
install -m 644 "$HERE/openclaw-config-sync.timer" "$UNIT_DIR/openclaw-config-sync.timer"
systemctl daemon-reload
systemctl enable --now openclaw-config-sync.timer
