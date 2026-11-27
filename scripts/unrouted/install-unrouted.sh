#!/usr/bin/env bash
# install-unrouted.sh - install the daily unrouted check (root): the timer that
# lists every owner ask and agent promise of yesterday's chats with no receipt,
# files each through the intake door and writes the state the fleet page reads
# (docs/runbook.md "Daily unrouted check", lifekit-stack#687):
#
#   sudo UNROUTED_FM_HOMES="<main home> <second-mate home>" \
#     bash /srv/lifekit-stack/scripts/unrouted/install-unrouted.sh
#
# UNROUTED_FM_HOMES is the space-separated list of firstmate homes whose task
# notes and captain-held items count as receipts; unset, the install is skipped.
# TASKS_AXI and CLAUDE_BIN (optional) are the absolute paths of tasks-axi and
# Claude Code; unset, each is looked up as the admin account (login shell, then
# an interactive one). No Claude Code, no install. REPO_DIR is the deploy
# checkout the unit runs from (default /srv/lifekit-stack).
#
# Then point the fleet publisher at the state: re-run its installer with
# FLEET_UNROUTED_STATE=/var/lib/lifekit-fleet/unrouted/unrouted.json.
#
# Tests set INSTALL_ROOT (a prefix for every installed path), SYSTEMCTL, SU and
# ADMIN_USER; an INSTALL_ROOT run needs no root and does not chown.

set -euo pipefail

INSTALL_ROOT="${INSTALL_ROOT:-}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
SU="${SU:-su}"
ADMIN_USER="${ADMIN_USER:-denys}"
REPO_DIR="${REPO_DIR:-/srv/lifekit-stack}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

read -r -a homes <<< "${UNROUTED_FM_HOMES:-}"
if ((${#homes[@]} == 0)); then
  echo "UNROUTED_FM_HOMES is not set; skipping the daily unrouted check." >&2
  exit 0
fi
home_args=""
for home in "${homes[@]}"; do
  [[ "$home" =~ ^/[A-Za-z0-9._/-]+$ ]] || {
    echo "firstmate home $home must be an absolute path of [A-Za-z0-9._/-]" >&2
    exit 1
  }
  home_args+="--home ${home} "
done
home_args="${home_args% }"
[[ "$REPO_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] || {
  echo "REPO_DIR must be an absolute path of [A-Za-z0-9._/-]" >&2
  exit 1
}
owner=(-o "$ADMIN_USER" -g "$ADMIN_USER")
if [[ -n "$INSTALL_ROOT" ]]; then
  owner=()
elif [[ $EUID -ne 0 ]]; then
  echo "Must run as root (sudo)." >&2
  exit 1
fi
ADMIN_HOME="$(getent passwd "$ADMIN_USER" | cut -d: -f6 || true)"
[[ -n "$INSTALL_ROOT" && -z "$ADMIN_HOME" ]] && ADMIN_HOME="/home/$ADMIN_USER"
[[ "$ADMIN_HOME" =~ ^/[A-Za-z0-9._/-]+$ ]] || {
  echo "no home directory for $ADMIN_USER" >&2
  exit 1
}

# A system unit's PATH has neither the node toolchain nor ~/.local/bin: resolve
# each tool as the admin account, as the intake shape installer does.
lookup() {
  local tool="$1" found=""
  if [[ -n "$INSTALL_ROOT" && "$SU" == su ]]; then
    found="$(command -v "$tool" || true)"
  else
    found="$("$SU" - "$ADMIN_USER" -c "command -v $tool" 2> /dev/null || true)"
    [[ "$found" == /* ]] ||
      found="$("$SU" - "$ADMIN_USER" -c "bash -ic \"command -v $tool\"" 2> /dev/null | tail -n 1 || true)"
  fi
  [[ "$found" == /* ]] && printf '%s\n' "$found"
}
TASKS_AXI="${TASKS_AXI:-$(lookup tasks-axi || true)}"
CLAUDE_BIN="${CLAUDE_BIN:-$(lookup claude || true)}"
[[ "$CLAUDE_BIN" =~ ^/[A-Za-z0-9._/-]+$ ]] || {
  echo "Claude Code not found for $ADMIN_USER; set CLAUDE_BIN to its absolute path of [A-Za-z0-9._/-]" >&2
  exit 1
}
if [[ -n "$TASKS_AXI" ]]; then
  [[ "$TASKS_AXI" =~ ^/[A-Za-z0-9._/-]+$ ]] || {
    echo "TASKS_AXI must be an absolute path of [A-Za-z0-9._/-]" >&2
    exit 1
  }
else
  echo "tasks-axi not found for $ADMIN_USER; held-/shape- receipts read as unchecked." >&2
fi
tool_dir="${TASKS_AXI%/*}"
UNIT_PATH="${tool_dir:+${tool_dir}:}${CLAUDE_BIN%/*}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

UNIT_DIR="$INSTALL_ROOT/etc/systemd/system"
install -d -m 755 "$UNIT_DIR"
install -d "${owner[@]}" -m 755 "$INSTALL_ROOT/var/lib/lifekit-fleet"
install -d "${owner[@]}" -m 750 "$INSTALL_ROOT/var/lib/lifekit-fleet/unrouted"
sed -e "s|__ADMIN_USER__|${ADMIN_USER}|g" -e "s|__ADMIN_HOME__|${ADMIN_HOME}|g" \
  -e "s|__TASKS_AXI__|${TASKS_AXI}|" -e "s|__UNIT_PATH__|${UNIT_PATH}|" \
  -e "s|__CLAUDE_BIN__|${CLAUDE_BIN}|" -e "s|__REPO_DIR__|${REPO_DIR}|" \
  -e "s|__HOME_ARGS__|${home_args}|" \
  "$HERE/lifekit-unrouted.service" > "$UNIT_DIR/lifekit-unrouted.service"
chmod 644 "$UNIT_DIR/lifekit-unrouted.service"
install -m 644 "$HERE/lifekit-unrouted.timer" "$UNIT_DIR/lifekit-unrouted.timer"
"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable --now lifekit-unrouted.timer
echo "daily unrouted check installed for ${homes[*]}"

# Record the installed commit (issue 689; the release gauge reads it).
bash "$(dirname "${BASH_SOURCE[0]}")/../lib/install-stamp.sh" unrouted
