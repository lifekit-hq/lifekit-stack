#!/usr/bin/env bash
# ensure-learnclaw-morning.sh — idempotently declare learnclaw's morning learning
# message (issue 609): one scheduled agent turn at about 07:15 Europe/Dublin,
# Monday to Saturday, that tells the owner in learnclaw's own Telegram chat
# (account `reading`, never Kit's `default`) what to open in LearnHarness today.
# Learning reaches the owner only from learnclaw (decision 2026-11-09), so Kit's
# today-brief and evening-plan-note carry no learning line.
#
# The message is at most four lines: (1) today's one LearnHarness session or
# goal and the gap it closes, (2) one practical output, (3) `mirror newest
# session <date>`, (4) the weekly count of LeetCode problems solved by hand
# against the weekly target, e.g. `Mon 0/20`: quoted from weekly-count.json, which
# scripts/ensure-learnclaw-weekcount.sh's 07:10 command job writes with the tested
# `daily.py weekfile` (issue 620), never counted by the model. Sunday is excluded by the schedule alone: this script
# refuses any cron expression that can fire on Sunday.
#
# Read-only by construction: the turn reads the LearnHarness mirror
# (JSON files in learnclaw's workspace, learnharness-mirror/, a copy that
# learnharness-sync refreshes from ~/.life-state/learnharness/data: the read
# tool reaches the workspace and the vault, not ~/.life-state, #611), the vault
# page domains/learning.md and weekly-count.json, and nothing else. Its tool allow-list is exactly `read`:
# no exec, no write, no sessions_spawn, no web, no message tool.
#
# Unlike leetcode-daily this needs a model turn, so it is an agent-turn payload
# on an isolated session. The model is the repo's cron model
# (scripts/lib/cron-model.sh).
#
# Why a script and not config: cron jobs live in the gateway's SQLite state,
# not in openclaw.json — same split as the other ensure-*.sh scripts. This
# script IS the git-side declaration of the job.
#
# Fail closed: after converging, the live row is re-read and the script exits 1
# (naming the field) unless the schedule is the declared one and cannot fire on
# Sunday, the timezone is the declared one, the agent is learnclaw, the payload is an agent turn with exactly the
# declared message, tool allow-list and model, the session is isolated, and
# delivery is announce to Telegram account `reading` at the owner chat.
#
# Failure: a run that fails outright is counted by the scheduler and the failure
# alert goes through kit's `default` Telegram account to the owner chat. The
# owner chat id is read from the host env file and never written to git.
#
# Run on the VPS host as lifekit, from the repo checkout, after the `reading`
# account is enabled and bound to learnclaw (docs/runbook.md, "Learnclaw
# morning learning message"):
#   /srv/lifekit-stack/scripts/ensure-learnclaw-morning.sh
#
# Env overrides:
#   LEARNCLAW_MORNING_CRON   cron expression (default: "15 7 * * 1,2,3,4,5,6";
#                            the check below refuses any that can fire on Sunday)
#   LEARNCLAW_MORNING_TZ     IANA tz (default: Europe/Dublin)
#   LEARNCLAW_MORNING_MODEL  the job's model (default: the repo's cron model)
#   LEARNCLAW_MORNING_CHAT   owner Telegram chat id for delivery and the failure
#                            alert (default: LIFEKIT_TELEGRAM_CHAT from ENV_FILE)
#   ENV_FILE                 host env file (default: /srv/lifekit-secrets/stack.env)
set -euo pipefail

# shellcheck source=scripts/lib/cron-check.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/cron-check.sh"
cron_check_args "$@"

# shellcheck source=scripts/lib/cron-model.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/cron-model.sh"
MODEL="${LEARNCLAW_MORNING_MODEL:-$(cron_model)}"
JOB_NAME=learnclaw-morning
AGENT=learnclaw
ACCOUNT=reading
JOB_TOOLS="read"
CRON_EXPR="${LEARNCLAW_MORNING_CRON:-15 7 * * 1,2,3,4,5,6}"
CRON_TZ="${LEARNCLAW_MORNING_TZ:-Europe/Dublin}"
COUNT_FILE=/home/node/.openclaw/agents/learnclaw/workspace/weekly-count.json
ENV_FILE="${ENV_FILE:-/srv/lifekit-secrets/stack.env}"

CHAT="${LEARNCLAW_MORNING_CHAT:-}"
if [ -z "$CHAT" ] && [ -r "$ENV_FILE" ]; then
  CHAT="$(sed -nE 's/^[[:space:]]*LIFEKIT_TELEGRAM_CHAT=["'"'"']?([0-9-]+)["'"'"']?[[:space:]]*$/\1/p' \
    "$ENV_FILE" | head -1)"
fi
if [ -z "$CHAT" ]; then
  cron_check_fail "no owner chat id: set LEARNCLAW_MORNING_CHAT, or LIFEKIT_TELEGRAM_CHAT in $ENV_FILE."
fi

# Refuse a schedule that can fire on Sunday, before touching the gateway.
# Field 5 of the expression: a list of weekdays, 0-7 (0 and 7 are Sunday).
python3 -I - "$CRON_EXPR" <<'PY'
import sys

fields = sys.argv[1].split()
if len(fields) != 5:
    sys.exit(f"refusing: cron {sys.argv[1]!r} is not a five-field expression")
dow = fields[4]
days = set()
for part in dow.split(","):
    if not part.isdigit() or int(part) > 7:
        sys.exit(f"refusing: day-of-week {dow!r} must be a plain list of days, no ranges or names")
    days.add(int(part) % 7)
if fields[2] != "*" or fields[3] != "*":
    sys.exit("refusing: day-of-month and month must be *")
if 0 in days or not days:
    sys.exit(f"refusing: cron {sys.argv[1]!r} can fire on Sunday")
PY

GATEWAY="$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway | head -n 1)"
[ -n "$GATEWAY" ] || cron_check_fail "no running openclaw-gateway container in compose project openclaw"

MESSAGE="Morning learning message for the owner. Read only: use the read tool, and only on the vault page /home/node/memory/domains/learning.md, the weekly count file named in line 4, and the JSON files of the LearnHarness mirror under /home/node/.openclaw/agents/learnclaw/workspace/learnharness-mirror (the sessions and the knowledge graph). Read nothing else, write nothing anywhere, record no row, send nothing yourself, start no subagent, open no web page, run no command.
Reply with at most 4 lines, plain text, no heading, no list marks, nothing before or after:
Line 1: today's one LearnHarness session to open, or the one goal on the learning page to work on, and the gap it closes (what the record shows is shaky or missing), at most 200 characters.
Line 2: one practical output the owner makes today, a single concrete thing, at most 200 characters.
Line 3: mirror newest session <YYYY-MM-DD>, the date of the newest session in the mirror (write: mirror unreadable, if you cannot read it).
Line 4: the weekly count line of problems solved by hand, quoted and never counted. Read the file ${COUNT_FILE}: JSON with for_date and line. When for_date is today's date from the session clock, write its line exactly as it is (for example Mon 0/20). When the file cannot be read, or for_date is any other date, write instead: <Mon|Tue|Wed|Thu|Fri|Sat> count unreadable, with today's weekday, and never write a number then. Do not look at the learning page for this line and do not work out a count yourself.
Never teach, explain or quiz. If you cannot read the learning page or the mirror, say so in line 1 instead of guessing."

# pick_json: first JSON object carrying the key (the CLI may interleave log lines).
pick_json() {
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
live_jobs() {
  docker exec "$GATEWAY" openclaw cron list --all --json 2>/dev/null | pick_json jobs || {
    echo "could not read the automation list from $GATEWAY" >&2; return 1; }
}
find_job_id() { # stdin = jobs JSON; $1 = job name
  python3 -c '
import json, sys
for job in json.load(sys.stdin)["jobs"]:
    if job.get("name") == sys.argv[1]:
        print(job["id"])
        break' "$1"
}

job_id="$(live_jobs | find_job_id "$JOB_NAME")" || cron_check_fail "could not read the automation list from $GATEWAY"
if [ -z "$job_id" ]; then
  cron_check_absent "$JOB_NAME"
  docker exec "$GATEWAY" openclaw cron add \
    --name "$JOB_NAME" \
    --display-name "Learnclaw morning learning message" \
    --description "At most 4 lines at 07:15 Monday to Saturday in learnclaw's own Telegram chat: today's LearnHarness session or goal and its gap, one practical output, the mirror's newest session date, the week's count of problems solved by hand against the weekly target. Read-only (LearnHarness mirror and domains/learning.md). Declared by scripts/ensure-learnclaw-morning.sh (source: lifekit-stack)." \
    --cron "$CRON_EXPR" --tz "$CRON_TZ" --exact \
    --agent "$AGENT" --session isolated \
    --message "$MESSAGE" --model "$MODEL" --tools "$JOB_TOOLS" --light-context \
    --timeout-seconds 300 \
    --announce --channel telegram --account "$ACCOUNT" --to "$CHAT" \
    --json >/dev/null
  job_id="$(live_jobs | find_job_id "$JOB_NAME")" || cron_check_fail "could not read the automation list from $GATEWAY"
  [ -n "$job_id" ] || { echo "cron add ran but no '$JOB_NAME' job is listed" >&2; exit 1; }
  echo "created cron '$JOB_NAME' ($job_id)"
fi

if [ "$CRON_CHECK" = 0 ]; then
  docker exec "$GATEWAY" openclaw cron edit "$job_id" \
    --cron "$CRON_EXPR" --tz "$CRON_TZ" --exact \
    --agent "$AGENT" --session isolated \
    --message "$MESSAGE" --model "$MODEL" --tools "$JOB_TOOLS" --light-context \
    --timeout-seconds 300 \
    --announce --no-best-effort-deliver \
    --channel telegram --account "$ACCOUNT" --to "$CHAT" \
    --failure-alert --failure-alert-after 1 \
    --failure-alert-mode announce --failure-alert-channel telegram \
    --failure-alert-account-id default --failure-alert-to "$CHAT" \
    >/dev/null
  # A row an operator paused is declared enabled again (a no-op when it already is).
  docker exec "$GATEWAY" openclaw cron enable "$job_id" >/dev/null
fi

# Fail closed: re-read the live row.
jobs="$(live_jobs)" || cron_check_fail "could not read the automation list from $GATEWAY"
printf '%s' "$jobs" | python3 -c '
import json, sys
job_id, agent, account, chat, expr, tz, tools, model, message = sys.argv[1:10]
job = next((j for j in json.load(sys.stdin)["jobs"] if j.get("id") == job_id), None)
if job is None:
    sys.exit(f"verify: job {job_id} is not listed after converging")
bad = []
live_agent = job.get("agentId") or job.get("agent")
if live_agent != agent:
    bad.append("agentId=" + repr(live_agent) + ", want " + repr(agent))
if job.get("enabled") is False:
    bad.append("enabled=False, want enabled")
payload = job.get("payload") or {}
if payload.get("kind") != "agentTurn":
    bad.append("payload.kind=" + repr(payload.get("kind")) + ", want agentTurn")
if payload.get("message") != message:
    bad.append("payload.message is not the declared message")
allow = payload.get("toolsAllow")
if not isinstance(allow, list) or sorted(allow) != sorted(tools.split(",")):
    bad.append("payload.toolsAllow=" + repr(allow) + ", want exactly " + repr(tools.split(",")))
# The gateway may record the inference account on the row (model@anthropic:api-key):
# the same model, so it agrees (scripts/lib/cron-model.sh).
if payload.get("model") not in (model, model + "@anthropic:api-key"):
    bad.append("payload.model=" + repr(payload.get("model")) + ", want " + repr(model))
if job.get("sessionTarget") != "isolated" or job.get("sessionKey"):
    bad.append("session " + repr(job.get("sessionKey") or job.get("sessionTarget")) + ", want isolated")
sched = job.get("schedule") or {}
live_expr = sched.get("expr") or sched.get("cron") or job.get("cron")
if live_expr != expr:
    bad.append("schedule " + repr(live_expr) + ", want " + repr(expr))
if sched.get("tz") != tz:
    bad.append("schedule.tz=" + repr(sched.get("tz")) + ", want " + repr(tz))
d = job.get("delivery") or {}
if d.get("mode") != "announce":
    bad.append("delivery.mode=" + repr(d.get("mode")) + ", want announce")
if d.get("channel") != "telegram":
    bad.append("delivery.channel=" + repr(d.get("channel")) + ", want telegram")
if d.get("accountId") != account:
    bad.append("delivery.accountId=" + repr(d.get("accountId")) + ", want " + repr(account))
if d.get("to") != chat:
    bad.append("delivery.to is not the owner chat")
if bad:
    sys.exit("refusing: learnclaw-morning is not the declared row: " + "; ".join(bad))
' "$job_id" "$AGENT" "$ACCOUNT" "$CHAT" "$CRON_EXPR" "$CRON_TZ" "$JOB_TOOLS" "$MODEL" "$MESSAGE" >&2
cron_check_agrees "$JOB_NAME" "$job_id"

echo "cron '$JOB_NAME' ($job_id) converged: $CRON_EXPR @ $CRON_TZ, agent:$AGENT isolated, model $MODEL, tools $JOB_TOOLS only, announce -> telegram:$ACCOUNT (verified), failure alert after 1 via telegram:default"
echo "verify: docker exec $GATEWAY openclaw cron show $job_id"
echo "test:   docker exec $GATEWAY openclaw cron run $job_id --expect-final --json   # one message in learnclaw's chat"
