#!/usr/bin/env bash
# install-learnharness-sync.sh - install the read-only pull of the owner's
# LearnHarness learning record from the PC into /var/lib/lifekit/learnharness/data,
# which the gateway sees as ~/.life-state/learnharness/data: learnclaw reads it
# with LearnHarness's own learnharness skill (docs/runbook.md, "learnclaw").
#
# Run once per host as the admin account (sudo), from the repo checkout:
#   LH_SYNC_HOST=<PC tailnet name> LH_SYNC_USER=<PC account> \
#     /srv/lifekit-stack/scripts/learnharness-sync/install-learnharness-sync.sh
# It creates the sync key (first run only), renders and enables the system
# units, and prints the one authorized_keys line to add on the PC: rrsync -ro
# limited to the LearnHarness data folder, from the tailnet only. Re-running
# converges; --print shows the rendered service only.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
: "${LH_SYNC_HOST:?set LH_SYNC_HOST to the PC tailnet name}"
: "${LH_SYNC_USER:?set LH_SYNC_USER to the PC account}"
KEYDIR="${LH_SYNC_KEYDIR:-/srv/lifekit-secrets/learnharness-sync}"
LH_DATA_ON_PC="${LH_DATA_ON_PC:-/mnt/c/Users/<windows user>/AppData/Roaming/LearnHarness/data}"
render() {
  sed -e "s#@HOST@#${LH_SYNC_HOST}#g" -e "s#@USER@#${LH_SYNC_USER}#g" \
    -e "s#@KEYDIR@#${KEYDIR}#g" "$here/learnharness-sync.service"
}
if [ "${1:-}" = "--print" ]; then
  render
  exit 0
fi
sudo install -d -o lifekit -g lifekit -m 0700 "$KEYDIR"
if ! sudo test -f "$KEYDIR/id_ed25519"; then
  sudo -u lifekit ssh-keygen -q -t ed25519 -N '' -C learnharness-sync -f "$KEYDIR/id_ed25519"
fi
sudo -u lifekit sh -c "ssh-keyscan -T 10 -t ed25519 '$LH_SYNC_HOST' > '$KEYDIR/known_hosts' 2>/dev/null" \
  || echo "known_hosts: the PC did not answer; re-run while it is on"
sudo install -d -o lifekit -g lifekit -m 0750 /var/lib/lifekit/learnharness /var/lib/lifekit/learnharness/data
render | sudo tee /etc/systemd/system/learnharness-sync.service >/dev/null
sudo install -m 0644 "$here/learnharness-sync.timer" /etc/systemd/system/learnharness-sync.timer
sudo systemctl daemon-reload
sudo systemctl enable --now learnharness-sync.timer
echo "installed: learnharness-sync.timer (every 10 minutes)"
echo "add this line to ${LH_SYNC_USER}'s ~/.ssh/authorized_keys on the PC:"
printf 'restrict,from="100.64.0.0/10",command="/usr/bin/rrsync -ro %s/" %s\n' \
  "$LH_DATA_ON_PC" "$(sudo cat "$KEYDIR/id_ed25519.pub")"
