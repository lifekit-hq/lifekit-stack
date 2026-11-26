#!/usr/bin/env bash
# install-fleet-publisher.sh — install the fleet publisher as a systemd timer
# (root). Same shape as the quota gauge. FM_HOMES is the space-separated list
# of fleet homes whose state/home-summary.json is published (default FM_HOME
# alone); unset means this box has no fleet and the install is skipped.
# bootstrap-vps.sh calls it; on a live box:
#
#   sudo FM_HOMES="<fleet home> [<fleet home> ...]" bash /srv/lifekit-stack/scripts/fleet-publisher/install-fleet-publisher.sh
#
# It also writes lifekit-fleet-scheduled.json beside the script: the timers and
# cron rows this checkout declares (scheduled.py manifest), which the summary's
# `scheduled` list is joined against, so a re-install picks up a new timer or
# declaring script (README "Scheduled work").
#
# TASKS_AXI (optional) is the absolute path of tasks-axi; unset, it is looked
# up as the admin account (login shell, then an interactive one for a node
# version manager set up in .bashrc).
#
# FLEET_PLAN_MAP (optional) is the publisher's "<repo>=<project> ..." alias
# list, which folds a repo spelling that names no project (host config, never
# in code) into its project's status-page row; unset or empty, the unit carries
# no FLEET_PLAN_MAP.
#
# FLEET_CLIP_STATS_FEED (optional) is the absolute path of clip's stats.jsonl
# (only its parent directory is read); it switches on the publisher's clip queue
# rows. On the VPS: /home/denys/clip-data/queue/stats.jsonl. Unset or empty, the
# unit carries no FLEET_CLIP_STATS_FEED and those rows stay null.
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
  echo "FM_HOMES (or FM_HOME) is not set; skipping the fleet publisher." >&2
  exit 0
fi
# Rendered into a quoted unit Environment= value: absolute, plain characters.
for home in "${homes[@]}"; do
  [[ "$home" =~ ^/[A-Za-z0-9._/-]+$ ]] || {
    echo "fleet home $home must be an absolute path of [A-Za-z0-9._/-]" >&2
    exit 1
  }
done
FM_HOMES="${homes[*]}"
FLEET_PLAN_MAP="${FLEET_PLAN_MAP:-}"
plan_map_env=""
if [[ -n "$FLEET_PLAN_MAP" ]]; then
  [[ "$FLEET_PLAN_MAP" =~ ^[A-Za-z0-9._/=\ -]+$ ]] || {
    echo "FLEET_PLAN_MAP must be \"<repo>=<project> ...\" of [A-Za-z0-9._/= -]" >&2
    exit 1
  }
  plan_map_env=" \"FLEET_PLAN_MAP=${FLEET_PLAN_MAP}\""
fi
FLEET_CLIP_STATS_FEED="${FLEET_CLIP_STATS_FEED:-}"
clip_feed_env=""
if [[ -n "$FLEET_CLIP_STATS_FEED" ]]; then
  [[ "$FLEET_CLIP_STATS_FEED" =~ ^/[A-Za-z0-9._/-]+$ ]] || {
    echo "FLEET_CLIP_STATS_FEED must be an absolute path of [A-Za-z0-9._/-]" >&2
    exit 1
  }
  clip_feed_env=" \"FLEET_CLIP_STATS_FEED=${FLEET_CLIP_STATS_FEED}\""
fi
owner=(-o "$ADMIN_USER" -g "$ADMIN_USER")
if [[ -n "$INSTALL_ROOT" ]]; then
  owner=()
elif [[ $EUID -ne 0 ]]; then
  echo "Must run as root (sudo)." >&2
  exit 1
fi

# tasks-axi lives in the admin account's node toolchain, which a system unit's
# PATH does not include: resolve it as the admin account and hand the unit its
# absolute path (the publisher adds its directory, and so node, to PATH). A
# plain login shell does not read the interactive-only setup (.bashrc) that
# puts a node version manager's bin on PATH, so fall back to an interactive one.
if [[ -z "${TASKS_AXI:-}" ]]; then
  if [[ -n "$INSTALL_ROOT" && "$SU" == su ]]; then
    TASKS_AXI="$(command -v tasks-axi || true)"
  else
    TASKS_AXI="$("$SU" - "$ADMIN_USER" -c 'command -v tasks-axi' 2> /dev/null || true)"
    [[ "$TASKS_AXI" == /* ]] ||
      TASKS_AXI="$("$SU" - "$ADMIN_USER" -c 'bash -ic "command -v tasks-axi"' 2> /dev/null | tail -n 1 || true)"
    [[ "$TASKS_AXI" == /* ]] || TASKS_AXI=""
  fi
fi
[[ -n "$TASKS_AXI" ]] || echo "tasks-axi not found for $ADMIN_USER; set TASKS_AXI to place boards by hold origin." >&2

UNIT_DIR="$INSTALL_ROOT/etc/systemd/system"
install -d "${owner[@]}" -m 755 "$INSTALL_ROOT/var/lib/node_exporter/textfile" "$INSTALL_ROOT/var/lib/lifekit-fleet"
install -d -m 755 "$INSTALL_ROOT/usr/local/bin" "$UNIT_DIR"
install -m 755 "$HERE/fleet-publisher.sh" "$INSTALL_ROOT/usr/local/bin/lifekit-fleet-publisher.sh"
# The status page helper; the publisher finds it beside itself under this name.
install -m 755 "$HERE/fleet_status.py" "$INSTALL_ROOT/usr/local/bin/lifekit-fleet-status.py"
# The stalled thresholds, the one source the status helper reads beside itself.
install -m 644 "$HERE/fleet-stalled.json" "$INSTALL_ROOT/usr/local/bin/lifekit-fleet-stalled.json"
# The decision follow-through config (brief time, back-filled items), read beside the helper.
install -m 644 "$HERE/fleet-decisions.json" "$INSTALL_ROOT/usr/local/bin/lifekit-fleet-decisions.json"
# The "Taken for you" feed helper, found the same way.
install -m 755 "$HERE/taken_feed.py" "$INSTALL_ROOT/usr/local/bin/lifekit-fleet-taken.py"
# The "Screen changes" feed helper, found the same way.
install -m 755 "$HERE/screen_changes.py" "$INSTALL_ROOT/usr/local/bin/lifekit-fleet-screens.py"
# The activity journal helper, found the same way.
install -m 755 "$HERE/activity_journal.py" "$INSTALL_ROOT/usr/local/bin/lifekit-fleet-journal.py"
install -m 755 "$HERE/scheduled.py" "$INSTALL_ROOT/usr/local/bin/lifekit-fleet-scheduled.py"
manifest_tmp="$(mktemp)"
python3 -I "$HERE/scheduled.py" manifest --repo "$(cd "$HERE/../.." && pwd)" > "$manifest_tmp"
install -m 644 "$manifest_tmp" "$INSTALL_ROOT/usr/local/bin/lifekit-fleet-scheduled.json"
rm -f "$manifest_tmp"
sed -e "s|__ADMIN_USER__|${ADMIN_USER}|" -e "s|__FM_HOMES__|${FM_HOMES}|" -e "s|__TASKS_AXI__|${TASKS_AXI}|" \
  -e "s|__FLEET_PLAN_MAP_ENV__|${plan_map_env}|" -e "s|__FLEET_CLIP_STATS_FEED_ENV__|${clip_feed_env}|" \
  "$HERE/lifekit-fleet-publisher.service" > "$UNIT_DIR/lifekit-fleet-publisher.service"
install -m 644 "$HERE/lifekit-fleet-publisher.timer" "$UNIT_DIR/lifekit-fleet-publisher.timer"
"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable --now lifekit-fleet-publisher.timer

# Record the installed commit (issue 689; the release gauge reads it).
bash "$(dirname "${BASH_SOURCE[0]}")/../lib/install-stamp.sh" fleet-publisher
