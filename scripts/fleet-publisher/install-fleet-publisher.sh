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
# The queue item and stalled sources (README "Queue items", "Stalled items"),
# each optional and carried into the unit only when set and non-empty:
#   FLEET_NEEDS_YOU_LEDGER  Kit's needs-you/open.json; unset, the default below
#                           (empty: none). Its directory is bound read-only into
#                           the unit at /run/lifekit-fleet-needs-you, since Kit's
#                           workspace is not readable by the admin account.
#   FLEET_FINANCE_CMD       the finance items command; on the VPS
#                           "python3 /usr/local/bin/lifekit-fleet-finance.py"
#                           (finance_items.py, installed here).
#   FLEET_FINANCE_OWNER     the owner's finance-sentry UserId (a GUID); the
#                           finance command lists only that user's rows and
#                           reads nothing without it.
#   FLEET_A2A_CMD           prints the open A2A threads; none exists yet.
#   FLEET_DEVCLAW_CMD       the devclaw items command; on the VPS
#                           "python3 /usr/local/bin/lifekit-fleet-devclaw.py"
#                           (devclaw_items.py, installed here: devclaw's own
#                           read-only `devclaw tasks --json` in its container).
#   FLEET_SCOUT_STATE       the scout's state dir; on the VPS
#                           /home/denys/.local/state/lifekit-scout.
#   FLEET_SCOUT_JUDGE       the firstmate home that judges the scout (its
#                           data/scout/judged.tsv and digest/); on the VPS the
#                           scout note's FM_INBOX_HOME (/etc/lifekit/scout.env).
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
KIT_NEEDS_YOU_LEDGER=/srv/openclaw/config/agents/kit/workspace/needs-you/open.json
NEEDS_YOU_BIND=/run/lifekit-fleet-needs-you
FLEET_NEEDS_YOU_LEDGER="${FLEET_NEEDS_YOU_LEDGER-$KIT_NEEDS_YOU_LEDGER}"
sources_env=""
needs_you_bind=""
if [[ -n "$FLEET_NEEDS_YOU_LEDGER" ]]; then
  [[ "$FLEET_NEEDS_YOU_LEDGER" =~ ^/[A-Za-z0-9._/-]+/[A-Za-z0-9._-]+$ ]] || {
    echo "FLEET_NEEDS_YOU_LEDGER must be an absolute file path of [A-Za-z0-9._/-]" >&2
    exit 1
  }
  # "-": a box without Kit's workspace starts the unit anyway (an empty ledger).
  needs_you_bind="\nBindReadOnlyPaths=-${FLEET_NEEDS_YOU_LEDGER%/*}:${NEEDS_YOU_BIND}"
  sources_env+=" \"FLEET_NEEDS_YOU_LEDGER=${NEEDS_YOU_BIND}/${FLEET_NEEDS_YOU_LEDGER##*/}\""
fi
for name in FLEET_FINANCE_CMD FLEET_A2A_CMD FLEET_DEVCLAW_CMD; do
  value="${!name:-}"
  [[ -n "$value" ]] || continue
  [[ "$value" =~ ^/?[A-Za-z0-9._/=:,\ -]+$ ]] || {
    echo "$name must be a command of [A-Za-z0-9._/=:, -]" >&2
    exit 1
  }
  sources_env+=" \"${name}=${value}\""
done
FLEET_FINANCE_OWNER="${FLEET_FINANCE_OWNER:-}"
if [[ -n "$FLEET_FINANCE_OWNER" ]]; then
  [[ "$FLEET_FINANCE_OWNER" =~ ^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$ ]] || {
    echo "FLEET_FINANCE_OWNER must be a GUID (the owner's finance-sentry UserId)" >&2
    exit 1
  }
  sources_env+=" \"FLEET_FINANCE_OWNER=${FLEET_FINANCE_OWNER}\""
fi
for name in FLEET_SCOUT_STATE FLEET_SCOUT_JUDGE; do
  value="${!name:-}"
  [[ -n "$value" ]] || continue
  [[ "$value" =~ ^/[A-Za-z0-9._/-]+$ ]] || {
    echo "$name must be an absolute path of [A-Za-z0-9._/-]" >&2
    exit 1
  }
  sources_env+=" \"${name}=${value}\""
done
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
# The finance items command (FLEET_FINANCE_CMD), read-only against finance-sentry.
install -m 755 "$HERE/finance_items.py" "$INSTALL_ROOT/usr/local/bin/lifekit-fleet-finance.py"
# The devclaw items command (FLEET_DEVCLAW_CMD), devclaw's read-only export.
install -m 755 "$HERE/devclaw_items.py" "$INSTALL_ROOT/usr/local/bin/lifekit-fleet-devclaw.py"
manifest_tmp="$(mktemp)"
python3 -I "$HERE/scheduled.py" manifest --repo "$(cd "$HERE/../.." && pwd)" > "$manifest_tmp"
install -m 644 "$manifest_tmp" "$INSTALL_ROOT/usr/local/bin/lifekit-fleet-scheduled.json"
rm -f "$manifest_tmp"
sed -e "s|__ADMIN_USER__|${ADMIN_USER}|" -e "s|__FM_HOMES__|${FM_HOMES}|" -e "s|__TASKS_AXI__|${TASKS_AXI}|" \
  -e "s|__FLEET_PLAN_MAP_ENV__|${plan_map_env}|" \
  -e "s|__FLEET_SOURCES_ENV__|${sources_env}|" -e "s|__NEEDS_YOU_BIND__|${needs_you_bind}|" \
  "$HERE/lifekit-fleet-publisher.service" > "$UNIT_DIR/lifekit-fleet-publisher.service"
install -m 644 "$HERE/lifekit-fleet-publisher.timer" "$UNIT_DIR/lifekit-fleet-publisher.timer"
"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable --now lifekit-fleet-publisher.timer

# Record the installed commit (issue 689; the release gauge reads it).
bash "$(dirname "${BASH_SOURCE[0]}")/../lib/install-stamp.sh" fleet-publisher
