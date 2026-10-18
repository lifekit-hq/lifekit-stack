#!/usr/bin/env bash
# install-memory-rotate.sh — install the monthly runtime-state rotation wrapper
# and its units (root). bootstrap-vps.sh calls it; on a live box run it alone
# to refresh /usr/local/bin and the units from the repo:
#
#   sudo bash /srv/lifekit-stack/scripts/rotate/install-memory-rotate.sh
#
# `--check` is read-only: exit 0 only when the installed copies match the repo
# and the timer is enabled. deploy.sh runs it as a report-only line (the deploy
# account has no sudo). The wrapper runs rotate-state.py and the logrotate
# config straight from the repo checkout, so only the wrapper and units need
# installing.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${MEMORY_ROTATE_UNIT_DIR:-/etc/systemd/system}"
BIN="${MEMORY_ROTATE_BIN:-/usr/local/bin/memory-rotate.sh}"
LIFEKIT_USER="${LIFEKIT_USER:-lifekit}"

if [[ "${1:-}" == "--check" ]]; then
  mismatch=0
  for pair in "$HERE/memory-rotate.sh:$BIN" \
    "$HERE/memory-rotate.service:$UNIT_DIR/memory-rotate.service" \
    "$HERE/memory-rotate.timer:$UNIT_DIR/memory-rotate.timer"; do
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
  state="$(systemctl is-enabled memory-rotate.timer 2>&1 || true)"
  echo "  memory-rotate.timer: $state"
  [[ "$state" == "enabled" ]] || mismatch=1
  exit "$mismatch"
fi

if [[ $EUID -ne 0 ]]; then
  echo "Must run as root (sudo)." >&2
  exit 1
fi

install -d -o "$LIFEKIT_USER" -g "$LIFEKIT_USER" -m 750 \
  /var/lib/lifekit/rotation /var/lib/lifekit/morning-brief
install -m 755 "$HERE/memory-rotate.sh" "$BIN"
install -m 644 "$HERE/memory-rotate.service" "$UNIT_DIR/memory-rotate.service"
install -m 644 "$HERE/memory-rotate.timer" "$UNIT_DIR/memory-rotate.timer"
systemctl daemon-reload
systemctl enable --now memory-rotate.timer
