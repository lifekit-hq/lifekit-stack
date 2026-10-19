#!/usr/bin/env bash
# Report files that could hold finance facts outside finance-sentry, in the
# finance agent's (Ledger's) Claude Code auto-memory dir and its workspace.
# Finance facts live only in finance-sentry; Ledger's AGENTS.md says so, the
# OpenClaw image locks auto-memory off, and this is the check that notices when
# a file turns up anyway. Read-only: it lists both trees through `docker exec`
# into the OpenClaw gateway and never deletes, moves or edits anything - the
# owner (or main) decides what to do with a hit. See docs/runbook.md "Ledger
# finance drift".
#
# The rule:
#   auto-memory  any file at all in AUTO_MEMORY_DIR is offending (auto-memory
#                is off, so the dir should stay empty or absent).
#   workspace    a file under WORKSPACE_DIR is offending unless its relative
#                path matches WORKSPACE_ALLOW below: the OpenClaw bootstrap
#                files, OpenClaw's own state, inbound chat media, vault daily
#                notes, installed skills, and Ledger's MCP helper scripts. A
#                new legitimate file is a PR that adds it to the list.
#
# Usage:
#   lifekit-ledger-drift.sh               check: one "<area>: <path>" line per
#                                         offending file; exit 0 clean, 1 drift
#   lifekit-ledger-drift.sh --textfile D  the same lines, plus an atomic write of
#                                         ledger_finance_drift.prom into D
#                                         (node-exporter's textfile collector);
#                                         exit 0 whenever the check ran
# Exit 2 when the check cannot run (no gateway container, docker exec fails,
# workspace missing); the textfile is then left untouched, so the timer's
# failed run (host-unit-down-or-failed) and textfile-collector-stale report it
# instead of an old reading passing for clean. Run as the admin account from
# lifekit-ledger-drift.timer (installed by install-host-gauges.sh).
#
#   ledger_finance_drift_file{area,path}  1 per offending file
#   ledger_finance_drift_files            number of offending files
set -euo pipefail
DOCKER="${DOCKER:-docker}"
AUTO_MEMORY_DIR="/home/node/.claude/projects/-home-node--openclaw-agents-finance-workspace/memory"
WORKSPACE_DIR="/home/node/.openclaw/agents/finance/workspace"
# Extended regexes, matched against the path relative to WORKSPACE_DIR.
WORKSPACE_ALLOW=(
  '^(AGENTS|SOUL|IDENTITY|USER|MEMORY|HEARTBEAT|TOOLS|BOOTSTRAP)\.md$'
  '^AGENTS\.md\.(bak\..+|pre-persona-install)$'
  '^\.openclaw/'
  '^openclaw-workspace-state\.json(\..+)?$'
  '^\.openclaw-cli-images/[^/]+$'
  '^media/inbound/'
  '^memory/(\.gitkeep|[0-9]{4}-[0-9]{2}-[0-9]{2}\.md)$'
  '^skills/'
  '^mcp/ibkr-flex-server\.mjs$'
  '^(mcp_call|mcp_scan|run_ledger_scan)\.js$'
)

textfile=""
case "${1:-}" in
  "") ;;
  --textfile)
    textfile="${2:?--textfile needs a directory}"
    ;;
  *)
    echo "usage: $0 [--textfile DIR]" >&2
    exit 2
    ;;
esac

die() {
  echo "ledger-drift: $*" >&2
  exit 2
}

cid="$("$DOCKER" ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway | head -n 1)" ||
  die "docker ps failed"
[[ -n "$cid" ]] || die "no running openclaw-gateway container"

listing="$(mktemp)"
tmp=""
trap 'rm -f "$listing" ${tmp:+"$tmp"}' EXIT

# list DIR REQUIRED -> NUL-separated relative file paths in $listing. A missing
# DIR is empty unless REQUIRED is 1. .git is skipped.
list() {
  # shellcheck disable=SC2016  # expanded by sh inside the container
  "$DOCKER" exec "$cid" sh -c '
    if [ ! -d "$1" ]; then [ "$2" = 1 ] && { echo "$1 is missing" >&2; exit 1; }; exit 0; fi
    find "$1" -mindepth 1 -name .git -prune -o \( -type f -o -type l \) -printf "%P\0"
  ' sh "$1" "$2" > "$listing" || die "listing $1 in the gateway failed"
}

allowed() {
  local re
  for re in "${WORKSPACE_ALLOW[@]}"; do
    [[ "$1" =~ $re ]] && return 0
  done
  return 1
}

# Prometheus label-value escaping; also keeps one stdout line per file.
escape() {
  local v="${1//\\/\\\\}"
  v="${v//\"/\\\"}"
  printf '%s' "${v//$'\n'/\\n}"
}

hits=()
list "$AUTO_MEMORY_DIR" 0
while IFS= read -r -d '' f; do
  hits+=("auto-memory" "$f")
done < "$listing"
list "$WORKSPACE_DIR" 1
while IFS= read -r -d '' f; do
  allowed "$f" || hits+=("workspace" "$f")
done < "$listing"

lines=()
for ((i = 0; i < ${#hits[@]}; i += 2)); do
  path="$(escape "${hits[i + 1]}")"
  echo "${hits[i]}: $path"
  lines+=("ledger_finance_drift_file{area=\"${hits[i]}\",path=\"$path\"} 1")
done
count=$((${#hits[@]} / 2))

if [[ -z "$textfile" ]]; then
  ((count == 0)) || exit 1
  exit 0
fi

tmp="$(mktemp "$textfile/.ledger_finance_drift.XXXXXX")"
{
  echo "# HELP ledger_finance_drift_file 1 per file in the finance agent's auto-memory dir or outside its workspace allowlist."
  echo "# TYPE ledger_finance_drift_file gauge"
  ((count == 0)) || printf '%s\n' "${lines[@]}" | sort
  echo "# HELP ledger_finance_drift_files Number of offending files in the finance agent's auto-memory dir and workspace."
  echo "# TYPE ledger_finance_drift_files gauge"
  echo "ledger_finance_drift_files $count"
} > "$tmp"
chmod 644 "$tmp"
mv -f "$tmp" "$textfile/ledger_finance_drift.prom"
