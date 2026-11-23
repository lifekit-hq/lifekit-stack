#!/usr/bin/env bash
# ensure-idle-lanes.sh — converge the host on the idle-lane check
# (scripts/idle-lanes): the script, the service and timer units, and the state
# directory the dashboard reads. Same shape as the other host timers: root
# applies it, `--check` only reports.
#
#   sudo IDLE_LANES_HOMES="<fleet home> [<fleet home> ...]" \
#     bash /srv/lifekit-stack/scripts/ensure-idle-lanes.sh
#   bash /srv/lifekit-stack/scripts/ensure-idle-lanes.sh --check
#
# `--check` is read-only: exit 0 only when the installed script and units match
# the repository and the timer is enabled. The home list is rendered into the
# unit; --check compares against the list the installed unit already carries
# unless IDLE_LANES_HOMES is set.
#
# Tests set INSTALL_ROOT (a prefix for every installed path), SYSTEMCTL, and
# ADMIN_USER; an INSTALL_ROOT run needs no root and does not chown.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/idle-lanes"
ADMIN_USER="${ADMIN_USER:-denys}"
INSTALL_ROOT="${INSTALL_ROOT:-}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
BIN="$INSTALL_ROOT/usr/local/bin/lifekit-idle-lanes.py"
UNIT_DIR="$INSTALL_ROOT/etc/systemd/system"
STATE_DIR="$INSTALL_ROOT/var/lib/lifekit-fleet/idle-lanes"
HOMES="${IDLE_LANES_HOMES:-}"

render() { # <homes> -> the service unit on stdout
  sed -e "s|__ADMIN_USER__|${ADMIN_USER}|" -e "s|__IDLE_LANES_HOMES__|$1|" \
    "$HERE/lifekit-idle-lanes.service"
}

if [[ "${1:-}" == "--check" ]]; then
  if [[ -z "$HOMES" && -f "$UNIT_DIR/lifekit-idle-lanes.service" ]]; then
    HOMES="$(sed -n 's/^Environment="IDLE_LANES_HOMES=\(.*\)"$/\1/p' "$UNIT_DIR/lifekit-idle-lanes.service")"
  fi
  mismatch=0
  want="$(mktemp)"
  trap 'rm -f "$want"' EXIT
  render "$HOMES" > "$want"
  for pair in "$HERE/idle_lanes.py:$BIN" \
    "$want:$UNIT_DIR/lifekit-idle-lanes.service" \
    "$HERE/lifekit-idle-lanes.timer:$UNIT_DIR/lifekit-idle-lanes.timer"; do
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
  state="$("$SYSTEMCTL" is-enabled lifekit-idle-lanes.timer 2>&1 || true)"
  echo "  lifekit-idle-lanes.timer: $state"
  [[ "$state" == "enabled" ]] || mismatch=1
  exit "$mismatch"
fi

if [[ -z "$INSTALL_ROOT" && $EUID -ne 0 ]]; then
  echo "Must run as root (sudo); --check reports without root." >&2
  exit 1
fi
[[ -n "$HOMES" ]] || {
  echo "IDLE_LANES_HOMES is not set (space-separated fleet homes)" >&2
  exit 1
}
# Rendered into a quoted unit Environment= value: absolute, plain characters.
read -r -a home_list <<< "$HOMES"
for home in "${home_list[@]}"; do
  [[ "$home" =~ ^/[A-Za-z0-9._/-]+$ ]] || {
    echo "fleet home $home must be an absolute path of [A-Za-z0-9._/-]" >&2
    exit 1
  }
done
HOMES="${home_list[*]}"

owner=(-o "$ADMIN_USER" -g "$ADMIN_USER")
[[ -z "$INSTALL_ROOT" ]] || owner=()
install -d "${owner[@]}" -m 755 "$STATE_DIR"
install -d -m 755 "$(dirname "$BIN")" "$UNIT_DIR"
install -m 755 "$HERE/idle_lanes.py" "$BIN"
render "$HOMES" > "$UNIT_DIR/lifekit-idle-lanes.service"
chmod 644 "$UNIT_DIR/lifekit-idle-lanes.service"
install -m 644 "$HERE/lifekit-idle-lanes.timer" "$UNIT_DIR/lifekit-idle-lanes.timer"
"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable --now lifekit-idle-lanes.timer
echo "idle-lane check installed for: $HOMES"
