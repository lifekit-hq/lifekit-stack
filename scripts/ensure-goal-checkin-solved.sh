#!/usr/bin/env bash
# ensure-goal-checkin-solved.sh — wire kit's Sunday 18:00 `goal-weekly-checkin`
# cron row to read the weekly LeetCode solved count from learnclaw instead of
# asking the owner for it.
#
# The count is learnclaw's (skills/leetcode-daily, `daily.py week`: problems
# solved by hand, Monday to Sunday, read from the artefact record). This script
# adds one marked block to the row's payload.message that tells the run to read
# it with that command, and gives the row `exec` when its tool allow-list lacks
# it. Nothing else in the row changes: schedule, delivery, model and the rest of
# the prompt stay as they are.
#
# Why a script: the row is host state whose prompt carries the owner's personal
# goal, which must not be copied into git (docs/PRIVATE.md). Like
# ensure-today-brief-fleet.sh, it edits the live row in place: it adds or replaces
# only the text between its own two markers, so re-running converges, and it
# leaves the rest of the prompt untouched. A row that does not exist is an error
# (the row is created by hand, docs/runbook.md "Daily LeetCode problem").
#
# Run on the VPS host as lifekit, from the repo checkout:
#   /srv/lifekit-stack/scripts/ensure-goal-checkin-solved.sh
# DRY_RUN=1 prints the new message instead of editing. Undo: delete the text
# between the markers, or re-apply the message saved first.
set -euo pipefail

# shellcheck source=scripts/lib/cron-check.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/cron-check.sh"
cron_check_args "$@"

JOB_NAME=goal-weekly-checkin
MARK_START="<<LEETCODE-COUNT"
MARK_END="LEETCODE-COUNT>>"
DAILY=/home/node/.openclaw/agents/learnclaw/workspace/skills/leetcode-daily/daily.py
BLOCK="${MARK_START}
LeetCode count: do not ask the owner how many LeetCode problems were solved by hand this week. It is learnclaw's record, read-only: run 'python3 ${DAILY} week' (one command, nothing else from that script) and take the number on its first line ('solved: <n> (Monday <date> to Sunday <date>, by hand)') as the week's count, with the titles it lists under it. If the command fails or prints no 'solved:' line, say once that the count is unavailable and ask for it.
${MARK_END}"

GATEWAY="$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway | head -n 1)"
[ -n "$GATEWAY" ] || cron_check_fail "no running openclaw-gateway container in compose project openclaw"

# The gateway CLI may interleave JSON log lines with its result: keep the first
# JSON object that carries the wanted key.
pick_json() { # $1 = key; stdin = CLI output
  python3 -c '
import json, sys
key, text = sys.argv[1], sys.stdin.read()
dec, i = json.JSONDecoder(), 0
while (i := text.find("{", i)) != -1:
    try:
        obj, end = dec.raw_decode(text, i)
    except ValueError:
        i += 1
        continue
    if isinstance(obj, dict) and key in obj:
        json.dump(obj, sys.stdout)
        sys.exit(0)
    i = end
sys.exit(1)' "$1"
}

jobs="$(docker exec "$GATEWAY" openclaw cron list --all --json 2>/dev/null | pick_json jobs)" || {
  cron_check_fail "could not read the automation list from $GATEWAY"; }

# plan -> stdout: JSON {"id","message","tools"|null,"changed"}; exit 12 when no row.
plan="$(printf '%s' "$jobs" | python3 -c '
import json, sys

name, start, end, block = sys.argv[1:5]
job = next((j for j in json.load(sys.stdin)["jobs"] if j.get("name") == name), None)
if job is None:
    sys.exit(12)
payload = job.get("payload") or {}
message = payload.get("message") or ""
i, j = message.find(start), message.find(end)
if i != -1 and j > i:
    new = message[:i] + block + message[j + len(end):]
elif i != -1 or j != -1:
    sys.exit("refusing: one LeetCode-count marker without the other in the live message")
else:
    new = message.rstrip("\n") + "\n\n" + block
tools = payload.get("toolsAllow")
new_tools = None
if isinstance(tools, list) and "exec" not in tools:
    new_tools = tools + ["exec"]
print(json.dumps({"id": job["id"], "message": new, "tools": new_tools,
                  "changed": new != message or new_tools is not None}))
' "$JOB_NAME" "$MARK_START" "$MARK_END" "$BLOCK")" || {
  rc=$?
  if [ "$rc" -eq 12 ]; then
    cron_check_absent "$JOB_NAME"
    echo "no '$JOB_NAME' automation row: it is host state (docs/runbook.md, \"Daily LeetCode problem\"); nothing changed" >&2
  fi
  exit 1
}

field() { printf '%s' "$plan" | python3 -c 'import json,sys; v=json.load(sys.stdin)[sys.argv[1]]; print("" if v is None else (",".join(v) if isinstance(v, list) else v), end="")' "$1"; }
job_id="$(field id)"
if [ "$(printf '%s' "$plan" | python3 -c 'import json,sys; print(json.load(sys.stdin)["changed"])')" != "True" ]; then
  cron_check_agrees "$JOB_NAME" "$job_id"
  echo "'$JOB_NAME' ($job_id) already reads the solved count from learnclaw; unchanged"
  [ "${DRY_RUN:-0}" = 1 ] || bash "$(dirname "${BASH_SOURCE[0]}")/lib/install-stamp.sh" goal-checkin-solved
  exit 0
fi
if [ "$CRON_CHECK" = 1 ]; then
  echo "drift: cron '$JOB_NAME' ($job_id) does not carry the solved-count block of $(basename "$0")" >&2
  exit 1
fi

message="$(field message)"
tools="$(field tools)"
if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "DRY_RUN: would set '$JOB_NAME' ($job_id) message to:"
  printf '%s\n' "$message"
  [ -z "$tools" ] || echo "DRY_RUN: and its tool allow-list to: $tools"
  exit 0
fi

args=(--message "$message")
[ -z "$tools" ] || args+=(--tools "$tools")
docker exec "$GATEWAY" openclaw cron edit "$job_id" "${args[@]}" >/dev/null
echo "'$JOB_NAME' ($job_id) now reads the weekly solved count from learnclaw (python3 daily.py week)"

# Record the installed commit (issue 689; the release gauge reads it).
bash "$(dirname "${BASH_SOURCE[0]}")/lib/install-stamp.sh" goal-checkin-solved
