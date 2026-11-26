#!/usr/bin/env bash
# install-ops-door.sh - the host half of the ops door (docs/runbook.md "Ops
# door"): the spool directory the receiver container writes Tier 2 requests
# into, and the ops-door-escalate timer that files them through
# `fleet-policy decide`. bootstrap-vps.sh does not call it; on a live box:
#
#   sudo bash /srv/lifekit-stack/scripts/ops-door/install-ops-door.sh
#
# `--check` is read-only: exit 0 only when the spool exists, the installed
# service matches the repository (placeholders substituted) and the timer is
# enabled. deploy.sh runs it as a report-only line (the deploy account has no
# sudo). The unit runs drain-escalations.py straight from the repo checkout.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "$HERE/../.." && pwd)}"
UNIT_DIR="${OPS_DOOR_UNIT_DIR:-/etc/systemd/system}"
SPOOL="${OPS_DOOR_SPOOL_DIR:-/var/lib/lifekit/ops-door/escalations}"
ADMIN_USER="${ADMIN_USER:-denys}"
OPS_DOOR_USER="${OPS_DOOR_USER:-$ADMIN_USER}"

render_service() {
  sed -e "s|__OPS_DOOR_USER__|${OPS_DOOR_USER}|g" -e "s|__REPO_DIR__|${REPO_DIR}|g" \
    "$HERE/ops-door-escalate.service"
}

if [[ "${1:-}" == "--check" ]]; then
  mismatch=0
  if [[ -d "$SPOOL" ]]; then echo "  $SPOOL: present"; else echo "  $SPOOL: absent"; mismatch=1; fi
  if render_service | cmp -s - "$UNIT_DIR/ops-door-escalate.service"; then
    echo "  $UNIT_DIR/ops-door-escalate.service: matches"
  else
    echo "  $UNIT_DIR/ops-door-escalate.service: absent or does not match the repository content"
    mismatch=1
  fi
  if cmp -s "$HERE/ops-door-escalate.timer" "$UNIT_DIR/ops-door-escalate.timer"; then
    echo "  $UNIT_DIR/ops-door-escalate.timer: matches"
  else
    echo "  $UNIT_DIR/ops-door-escalate.timer: absent or does not match the repository content"
    mismatch=1
  fi
  state="$(systemctl is-enabled ops-door-escalate.timer 2>&1 || true)"
  echo "  ops-door-escalate.timer: $state"
  [[ "$state" == "enabled" ]] || mismatch=1
  exit "$mismatch"
fi

if [[ $EUID -ne 0 ]]; then
  echo "Must run as root (sudo)." >&2
  exit 1
fi

# World-writable + sticky: the receiver container's user (uid 1000) writes new
# request files, and the operator account owns the directory, so it alone can
# move them to done/. The files hold alert names and issue URLs, no secrets.
install -d -o "$OPS_DOOR_USER" -m 755 /var/lib/lifekit/ops-door
install -d -o "$OPS_DOOR_USER" -m 1777 "$SPOOL"
install -d -o "$OPS_DOOR_USER" -m 755 "$SPOOL/done"
render_service > "$UNIT_DIR/ops-door-escalate.service"
chmod 644 "$UNIT_DIR/ops-door-escalate.service"
install -m 644 "$HERE/ops-door-escalate.timer" "$UNIT_DIR/ops-door-escalate.timer"
systemctl daemon-reload
systemctl enable --now ops-door-escalate.timer

# Record the installed commit (issue 689; the release gauge reads it).
bash "$(dirname "${BASH_SOURCE[0]}")/../lib/install-stamp.sh" ops-door-escalate
