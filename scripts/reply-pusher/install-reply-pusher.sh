#!/usr/bin/env bash
# install-reply-pusher.sh - install the reply pusher: a oneshot service plus a
# .path unit that starts it when a reply lands in a pinned firstmate home
# (root). FM_HOMES is the space-separated list of homes to watch (default
# FM_HOME alone); unset means this box has no fleet and the install is skipped.
# bootstrap-vps.sh calls it; on a live box:
#
#   sudo FM_HOMES="<fleet home> [<fleet home> ...]" bash /srv/lifekit-stack/scripts/reply-pusher/install-reply-pusher.sh
#
# The install writes each home's starting cursor at its newest existing reply
# (never moving a cursor that is already there), so no history replays, and only
# then enables the units. The hook mapping it pushes to ships separately in
# compose/openclaw-gateway/platform.patch.json (docs/runbook.md "Reply pusher").
#
# Tests set INSTALL_ROOT (a prefix for every installed path), SYSTEMCTL and SU; an
# INSTALL_ROOT run needs no root and does not chown.

set -euo pipefail

ADMIN_USER="${ADMIN_USER:-denys}"
FM_HOMES="${FM_HOMES:-${FM_HOME:-}}"
INSTALL_ROOT="${INSTALL_ROOT:-}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
SU="${SU:-su}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

read -r -a homes <<< "$FM_HOMES"
if ((${#homes[@]} == 0)); then
  echo "FM_HOMES (or FM_HOME) is not set; skipping the reply pusher." >&2
  exit 0
fi
# Rendered into quoted unit values and a shell command: absolute, plain characters,
# and a directory name the pusher accepts as a home id.
declare -A seen=()
for home in "${homes[@]}"; do
  [[ "$home" =~ ^/[A-Za-z0-9._/-]+$ ]] || {
    echo "fleet home $home must be an absolute path of [A-Za-z0-9._/-]" >&2
    exit 1
  }
  [[ "${home##*/}" =~ ^[a-z0-9][a-z0-9-]{0,63}$ ]] || {
    echo "fleet home $home: its directory name must match [a-z0-9][a-z0-9-]*" >&2
    exit 1
  }
  [[ -z "${seen[${home##*/}]:-}" ]] || {
    echo "fleet homes share the directory name ${home##*/}" >&2
    exit 1
  }
  seen[${home##*/}]=1
done
FM_HOMES="${homes[*]}"
owner=(-o "$ADMIN_USER" -g "$ADMIN_USER")
if [[ -n "$INSTALL_ROOT" ]]; then
  owner=()
elif [[ $EUID -ne 0 ]]; then
  echo "Must run as root (sudo)." >&2
  exit 1
fi

UNIT_DIR="$INSTALL_ROOT/etc/systemd/system"
STATE_DIR="$INSTALL_ROOT/var/lib/lifekit-reply-pusher"
BIN_DIR="$INSTALL_ROOT/usr/local/libexec/lifekit-reply-pusher"
install -d -m 755 "$BIN_DIR" "$UNIT_DIR"
install -d "${owner[@]}" -m 700 "$STATE_DIR"
install -m 755 "$HERE/reply_pusher.py" "$BIN_DIR/reply_pusher.py"
sed -e "s|__ADMIN_USER__|${ADMIN_USER}|" -e "s|__FM_HOMES__|${FM_HOMES}|" \
  "$HERE/lifekit-reply-pusher.service" > "$UNIT_DIR/lifekit-reply-pusher.service"
chmod 644 "$UNIT_DIR/lifekit-reply-pusher.service"
while IFS= read -r line; do
  if [[ "$line" == __PATH_CHANGED__ ]]; then
    for home in "${homes[@]}"; do
      printf 'PathChanged=%s/state/inbox/.replies\n' "$home"
    done
  else
    printf '%s\n' "$line"
  fi
done < "$HERE/lifekit-reply-pusher.path" > "$UNIT_DIR/lifekit-reply-pusher.path"
chmod 644 "$UNIT_DIR/lifekit-reply-pusher.path"

# Starting cursors, as the admin account (it owns the homes and the state dir).
# A home that already has one keeps it.
"$SU" - "$ADMIN_USER" -c "REPLY_PUSHER_HOMES='${FM_HOMES}' REPLY_PUSHER_STATE='${STATE_DIR}' /usr/bin/python3 '${BIN_DIR}/reply_pusher.py' baseline"

"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable lifekit-reply-pusher.service
"$SYSTEMCTL" enable --now lifekit-reply-pusher.path
