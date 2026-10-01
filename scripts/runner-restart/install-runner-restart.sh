#!/usr/bin/env bash
# install-runner-restart.sh — give every installed actions.runner.* systemd
# unit a Restart=on-failure drop-in (root). svc.sh installs the runner units
# with Restart=no, so a crashed runner stays down until someone notices.
# Idempotent: rewrites the same drop-in each run, and does not restart a
# running runner (the setting applies from its next start).
# bootstrap-vps.sh calls it; on a live box run it alone:
#
#   sudo bash /srv/lifekit-stack/scripts/runner-restart/install-runner-restart.sh

set -euo pipefail

UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"

while read -r unit _; do
  [[ "$unit" == actions.runner.*.service ]] || continue
  install -d -m 755 "$UNIT_DIR/${unit}.d"
  printf '[Service]\nRestart=on-failure\nRestartSec=10\n' \
    > "$UNIT_DIR/${unit}.d/restart.conf"
done < <("$SYSTEMCTL" list-unit-files 'actions.runner.*.service' --no-legend --no-pager)
"$SYSTEMCTL" daemon-reload
