#!/usr/bin/env bash
# ensure-leetcode-daily.sh — idempotently declare learnclaw's daily LeetCode
# problem: one scheduled agent turn at the home-day 17:30 interview-problem slot
# that sends the owner one problem, name and link only, in learnclaw's own
# Telegram chat (account `reading`, never Kit's `default`).
#
# What the turn does is in skills/leetcode-daily/daily.py, not in the prompt:
# the turn runs `daily.py next --record` and sends what it prints. The script
# decides the day (Tuesday and Thursday are office days, Sunday is rest: it
# prints SKIP and the turn replies NO_REPLY, so those days send nothing even if
# the schedule were ever widened; an error or empty output fails the turn so the
# failure alert fires, and is never answered NO_REPLY), takes the first problem in the curated list
# (skills/leetcode-daily/problems.json) that has no row in the artefact record,
# and appends the offered row. A problem that is done, skipped or offered never
# comes back. The prompt carries no state: the day and the record are read by
# the script at run time (docs/runbook.md, "The payload is stateless").
#
# Why a script and not config: cron jobs live in the gateway's SQLite state,
# not in openclaw.json — same split as the other ensure-*.sh scripts. This
# script IS the git-side declaration of the job.
#
# The model carries its inference account (`provider/model@anthropic:api-key`,
# scripts/lib/cron-model.sh, cron_model_account): a cron row without a recorded
# account fails to run.
#
# Fail closed: after converging, the live row is re-read and the script exits 1
# (naming the field) unless the schedule only fires on home days, the agent is
# learnclaw, the model value is the one declared, the tool allow-list is exactly
# TOOLS, and delivery is announce to Telegram account `reading` at the owner
# chat. A row that could deliver through Kit's account fails the run instead of
# pinging the owner from the wrong bot.
#
# Failure: a run that fails outright (including a daily.py error) is counted by
# the scheduler; the failure alert goes through kit's `default` Telegram account to the owner chat, the
# route the digest and the pulse use. The owner chat id is read from the host
# env file and never written to git.
#
# Run on the VPS host as lifekit, from the repo checkout, after the `reading`
# Telegram account is enabled and bound to learnclaw (docs/runbook.md, "Daily
# LeetCode problem"):
#   /srv/lifekit-stack/scripts/ensure-leetcode-daily.sh
#
# Env overrides:
#   LEETCODE_DAILY_CRON   cron expression (default: "30 17 * * 1,3,5,6": Mon,
#                         Wed, Fri, Sat; the check below refuses Tue, Thu, Sun)
#   LEETCODE_DAILY_TZ     IANA tz (default: Europe/Dublin)
#   LEETCODE_DAILY_CHAT   owner Telegram chat id for delivery and the failure
#                         alert (default: LIFEKIT_TELEGRAM_CHAT from ENV_FILE)
#   ENV_FILE              host env file (default: /srv/lifekit-secrets/stack.env)
set -euo pipefail

# shellcheck source=scripts/lib/cron-model.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/cron-model.sh"

JOB_NAME=leetcode-daily
AGENT=learnclaw
ACCOUNT=reading
CRON_EXPR="${LEETCODE_DAILY_CRON:-30 17 * * 1,3,5,6}"
CRON_TZ="${LEETCODE_DAILY_TZ:-Europe/Dublin}"
# exec runs daily.py; read is there for its output files. No message, browser,
# web or write tool: the reply itself is the delivery.
TOOLS="exec,read"
MODEL="$(cron_model_account)"
ENV_FILE="${ENV_FILE:-/srv/lifekit-secrets/stack.env}"

CHAT="${LEETCODE_DAILY_CHAT:-}"
if [ -z "$CHAT" ] && [ -r "$ENV_FILE" ]; then
  CHAT="$(sed -nE 's/^[[:space:]]*LIFEKIT_TELEGRAM_CHAT=["'"'"']?([0-9-]+)["'"'"']?[[:space:]]*$/\1/p' \
    "$ENV_FILE" | head -1)"
fi
if [ -z "$CHAT" ]; then
  echo "no owner chat id: set LEETCODE_DAILY_CHAT, or LIFEKIT_TELEGRAM_CHAT in $ENV_FILE." >&2
  exit 1
fi

# Refuse a schedule that can fire on an office day or the rest day, before
# touching the gateway. Field 5 of the expression: a list of weekdays, 0-7.
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
bad = sorted(days & {0, 2, 4})  # cron: 0 Sunday, 2 Tuesday, 4 Thursday
if bad or not days:
    sys.exit(f"refusing: cron {sys.argv[1]!r} fires on an office day or Sunday (cron day numbers {bad})")
PY

GATEWAY="$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway | head -n 1)"
[ -n "$GATEWAY" ] || { echo "no running openclaw-gateway container in compose project openclaw" >&2; exit 1; }

DAILY=/home/node/.openclaw/agents/learnclaw/workspace/skills/leetcode-daily/daily.py
MESSAGE="Daily LeetCode problem. Run exactly one command: 'python3 ${DAILY} next --record' and read what it prints. If its first line is SEND, your whole final reply is the next two lines exactly as printed, the problem name and then its link: no greeting, no hint, no pattern name, no solution, nothing before or after. If its first line starts with SKIP, your entire final reply is exactly NO_REPLY. If the command errors, exits non-zero, prints a traceback or anything on stderr, or prints nothing, the run has failed: end the turn as failed, send no reply at all, and never reply NO_REPLY or any error text. Call no other tool and edit nothing."

# The gateway image has no jq, and once logging.consoleStyle=json is live the
# CLI may interleave JSON log lines with its JSON result. So: scan the output
# for JSON values and keep the first object that carries the wanted key.
pick_json() { # $1 = required top-level key; stdin = CLI output; stdout = that object
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

job_id="$(live_jobs | find_job_id "$JOB_NAME")" || exit 1
if [ -z "$job_id" ]; then
  docker exec "$GATEWAY" openclaw cron add \
    --name "$JOB_NAME" \
    --display-name "Daily LeetCode problem" \
    --description "One LeetCode problem (name and link) on home days at 17:30, in learnclaw's own Telegram chat; picked and recorded by skills/leetcode-daily. Declared by scripts/ensure-leetcode-daily.sh (source: lifekit-stack)." \
    --cron "$CRON_EXPR" --tz "$CRON_TZ" --exact \
    --agent "$AGENT" --session isolated \
    --message "$MESSAGE" \
    --model "$MODEL" \
    --tools "$TOOLS" \
    --light-context \
    --timeout-seconds 300 \
    --announce --channel telegram --account "$ACCOUNT" --to "$CHAT" \
    --json >/dev/null
  job_id="$(live_jobs | find_job_id "$JOB_NAME")" || exit 1
  [ -n "$job_id" ] || { echo "cron add ran but no '$JOB_NAME' job is listed" >&2; exit 1; }
  echo "created cron '$JOB_NAME' ($job_id)"
fi

docker exec "$GATEWAY" openclaw cron edit "$job_id" \
  --cron "$CRON_EXPR" --tz "$CRON_TZ" --exact \
  --agent "$AGENT" --session isolated \
  --message "$MESSAGE" \
  --model "$MODEL" \
  --tools "$TOOLS" \
  --light-context \
  --timeout-seconds 300 \
  --announce --no-best-effort-deliver \
  --channel telegram --account "$ACCOUNT" --to "$CHAT" \
  --failure-alert --failure-alert-after 1 \
  --failure-alert-mode announce --failure-alert-channel telegram \
  --failure-alert-account-id default --failure-alert-to "$CHAT" \
  >/dev/null

# Fail closed: re-read the live row.
live_jobs | python3 -c '
import json, sys
job_id, agent, account, chat, model, tools, expr = sys.argv[1:8]
job = next((j for j in json.load(sys.stdin)["jobs"] if j.get("id") == job_id), None)
if job is None:
    sys.exit(f"verify: job {job_id} is not listed after converging")
bad = []
live_agent = job.get("agentId") or job.get("agent")
if live_agent != agent:
    bad.append("agentId=" + repr(live_agent) + ", want " + repr(agent))
payload = job.get("payload") or {}
if payload.get("model") != model:
    bad.append("payload.model=" + repr(payload.get("model")) + ", want " + repr(model))
allow = payload.get("toolsAllow")
if not isinstance(allow, list) or sorted(allow) != sorted(tools.split(",")):
    bad.append("payload.toolsAllow=" + repr(allow) + ", want exactly " + repr(tools))
sched = job.get("schedule") or {}
live_expr = sched.get("expr") or sched.get("cron") or job.get("cron")
if live_expr != expr:
    bad.append("schedule " + repr(live_expr) + ", want " + repr(expr))
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
    sys.exit("refusing: leetcode-daily is not the declared row: " + "; ".join(bad))
' "$job_id" "$AGENT" "$ACCOUNT" "$CHAT" "$MODEL" "$TOOLS" "$CRON_EXPR" >&2

echo "cron '$JOB_NAME' ($job_id) converged: $CRON_EXPR @ $CRON_TZ, agent:$AGENT isolated, model $MODEL, tools $TOOLS, announce -> telegram:$ACCOUNT (verified), failure alert after 1 via telegram:default"
echo "verify: docker exec $GATEWAY openclaw cron show $job_id"
echo "        docker exec $GATEWAY python3 $DAILY next --date <a Tuesday>   # SKIP office day (Tuesday), nothing recorded"
echo "        docker exec $GATEWAY python3 $DAILY status"
