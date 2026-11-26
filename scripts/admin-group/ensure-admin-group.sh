#!/usr/bin/env bash
# Put the admin account in the lifekit group, then verify it is there.
#
# /srv/lifekit-stack and /var/lib/lifekit are lifekit:lifekit 0750, so the
# admin account - which runs the release gauge and installers without sudo -
# can read the deploy checkout and write install stamps only as a member of
# the lifekit group (docs/runbook.md "Weekly release gauge").
#
#   ADMIN_USER=<account> [LIFEKIT_GROUP=lifekit] ensure-admin-group.sh [--check]
#
# Without --check (as root, from bootstrap-vps.sh) it adds the membership when
# missing. Both modes exit 1, naming the fix, when ADMIN_USER is not a member.
set -euo pipefail

: "${ADMIN_USER:?set ADMIN_USER to the admin account}"
LIFEKIT_GROUP="${LIFEKIT_GROUP:-lifekit}"
check_only=0
[[ "${1:-}" == --check ]] && check_only=1

in_group() { id -nG "$ADMIN_USER" | tr ' ' '\n' | grep -qxF "$LIFEKIT_GROUP"; }

if ! in_group && ((!check_only)); then
  usermod -aG "$LIFEKIT_GROUP" "$ADMIN_USER"
fi

if ! in_group; then
  echo "admin group: $ADMIN_USER is not in the $LIFEKIT_GROUP group, so the release gauge and installer stamps cannot read /srv/lifekit-stack or write /var/lib/lifekit; fix: sudo usermod -aG $LIFEKIT_GROUP $ADMIN_USER" >&2
  exit 1
fi
echo "admin group: $ADMIN_USER is in the $LIFEKIT_GROUP group"
