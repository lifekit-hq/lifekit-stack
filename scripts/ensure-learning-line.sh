#!/usr/bin/env bash
# ensure-learning-line.sh — make the daily plan's one learning objective line
# come from learnclaw (issue 525). Edits two kit cron rows, one `openclaw cron
# edit` each:
#   evening-plan-note (21:00): step 4 asks learnclaw for the line with a
#     subagent run instead of reading the learning page and the LearnHarness
#     mirror; the row's tool allow-list gains sessions_spawn + sessions_yield.
#   today-brief (08:30): step 7 copies the line from the daily note and never
#     derives it.
#
# Why a script: both rows are host state whose prompts carry the owner's
# personal plan, which must not be copied into git (docs/PRIVATE.md). Like
# ensure-today-brief-fleet.sh, this replaces text at generic anchors only,
# refuses (exit 1, that row untouched) when an anchor is missing, and leaves a
# row that already carries the new text alone, so re-running converges.
#
# Run on the VPS host as lifekit, from the repo checkout:
#   /srv/lifekit-stack/scripts/ensure-learning-line.sh
# DRY_RUN=1 prints each new message instead of editing. Undo: re-apply the old
# message saved first (docs/runbook.md "Learning objective line from learnclaw").
set -euo pipefail

GATEWAY="$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway | head -n 1)"
[ -n "$GATEWAY" ] || { echo "no running openclaw-gateway container in compose project openclaw" >&2; exit 1; }

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
  echo "could not read the automation list from $GATEWAY" >&2; exit 1; }

# plan <row name> -> stdout: JSON {"id","message","tools"|null}; exit 10 already
# done, 11 anchor missing, 12 no such row.
plan() {
  printf '%s' "$jobs" | python3 -c '
import json, re, sys

name = sys.argv[1]

EVENING_STEP4 = (
    "4. Learning objective: it comes from learnclaw, never from your own reading of the learning page or the "
    "LearnHarness mirror. Start one learnclaw run with sessions_spawn (agentId learnclaw, task below), wait for "
    "its result with sessions_yield, and use its one line as the objective. Task to send, with <tomorrow Weekday "
    "D Mon> filled in: \"Daily plan for the owner, <tomorrow Weekday D Mon>. Give the one learning objective line "
    "for that day. Read the LearnHarness record (learnharness skill, --data-dir ~/.life-state/learnharness/data) "
    "and the learning page, read-only. Reply with exactly one line of at most 200 characters: <the LearnHarness "
    "session to open, or the gap it addresses> | mirror newest session <YYYY-MM-DD>, where the date is the newest "
    "session in the mirror (write '"'"'mirror unreadable'"'"' in its place if you cannot read it). Write nothing "
    "anywhere and record no row.\" If learnclaw is unreachable, fails, or answers with anything but one such "
    "line, write the single line \"Learning objective unavailable (learnclaw)\" and do not fill the gap "
    "yourself. Never write into LearnHarness.\n"
)
EVENING_OBJECTIVE = (
    "(step 4'"'"'s line, copied verbatim)"
)
EVENING_HOME = (
    "home days: 07:30 and the 08:30 block work toward that objective, LeetCode problems by hand at 17:30."
)
BRIEF_STEP7 = (
    "Keep its \"## Plan\" block, its \"## Learning objective\" line (written by the evening plan job from "
    "learnclaw; copy it verbatim, with its mirror date) and the practical output. Never derive the learning "
    "objective yourself and never read the learning page or the LearnHarness mirror for it. If the note is "
    "missing, say once \"Plan note missing\" and build a short plan from today'"'"'s calendar alone (mark "
    "assumed blocks \"(assumed)\", and write \"Learn: not in the plan note\")."
)
NEW_TOOLS = ["sessions_spawn", "sessions_yield"]

job = next((j for j in json.load(sys.stdin)["jobs"] if j.get("name") == name), None)
if job is None:
    sys.exit(12)
payload = job.get("payload") or {}
msg = payload.get("message") or ""
tools = payload.get("toolsAllow")

def out(message, tools):
    json.dump({"id": job["id"], "message": message, "tools": tools}, sys.stdout)

if name == "evening-plan-note":
    missing = [t for t in NEW_TOOLS if tools is not None and t not in tools]
    if EVENING_STEP4 in msg and not missing:
        sys.exit(10)
    i, j = msg.find("4. Learning goal:"), msg.find("5. Fleet and week:")
    has_new = EVENING_STEP4 in msg
    if not has_new and (i < 0 or j < i):
        sys.exit(11)
    m = msg if has_new else msg[:i] + EVENING_STEP4 + msg[j:]
    if not has_new:
        # the learning page is no longer read by this row
        m = m.replace("goals.md, /home/node/memory/domains/learning.md and /home/node/memory/domains/career.md",
                      "goals.md and /home/node/memory/domains/career.md", 1)
        if "(the picked goals)" not in m:
            sys.exit(11)
        m = m.replace("(the picked goals)", EVENING_OBJECTIVE, 1)
        m, n = re.subn(r"home days: 07:30 Track A goal.*? block\.", EVENING_HOME, m, count=1)
        if n != 1:
            sys.exit(11)
    out(m, None if tools is None else tools + [t for t in NEW_TOOLS if t not in tools])
elif name == "today-brief":
    if BRIEF_STEP7 in msg:
        sys.exit(10)
    m, n = re.subn(
        r"Keep its \"## Plan\" block, the learning objective and the practical output\. If the note is missing, "
        r".*?\(mark assumed blocks \"\(assumed\)\"\)\.",
        lambda _: BRIEF_STEP7, msg, count=1, flags=re.S,
    )
    if n != 1:
        sys.exit(11)
    out(m, None)
else:
    sys.exit(12)
' "$1"
}

rc=0
for name in evening-plan-note today-brief; do
  status=0
  result="$(plan "$name")" || status=$?
  case "$status" in
    0) ;;
    10) echo "'$name' already carries the learnclaw learning line; nothing to do"; continue ;;
    11)
      echo "'$name': the prompt no longer has the text this script anchors on; row left untouched." >&2
      echo "Make the change by hand (scripts/ensure-learning-line.sh holds its text)." >&2
      rc=1; continue ;;
    12) echo "no '$name' automation row. It is host state, not applied by deploy." >&2; rc=1; continue ;;
    *) echo "could not build the new message for '$name' (python exit $status)" >&2; rc=1; continue ;;
  esac
  id="$(printf '%s' "$result" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
  message="$(printf '%s' "$result" | python3 -c 'import json,sys; sys.stdout.write(json.load(sys.stdin)["message"])')"
  tools="$(printf '%s' "$result" | python3 -c 'import json,sys; t=json.load(sys.stdin)["tools"]; sys.stdout.write(",".join(t) if t else "")')"
  if [ "${DRY_RUN:-0}" = 1 ]; then
    printf '== %s (%s) new message ==\n%s\n' "$name" "$id" "$message"
    [ -z "$tools" ] || printf '== tools ==\n%s\n' "$tools"
    continue
  fi
  tool_args=()
  [ -z "$tools" ] || tool_args=(--tools "$tools")
  docker exec "$GATEWAY" openclaw cron edit "$id" --message "$message" "${tool_args[@]}" >/dev/null
  echo "'$name' ($id) now takes the learning line from learnclaw"
  echo "verify: docker exec $GATEWAY openclaw cron show $id"
done
exit "$rc"
