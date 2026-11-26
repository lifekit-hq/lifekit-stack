#!/usr/bin/env bash
# ensure-learnclaw-weekcount.sh — idempotently declare the job that writes the
# weekly LeetCode count line for learnclaw's morning message (issue 620).
#
# The morning message (ensure-learnclaw-morning.sh) is a read-only model turn
# with no exec, so it cannot count and nothing checked a number the model
# counted itself. This job is a command payload at 07:10, five minutes before
# it: it runs `daily.py weekfile`, the tested counter behind `daily.py week`
# (latest row per slug, status done, dated Monday to Sunday), and replaces
# weekly-count.json in learnclaw's workspace with {"for_date", "line"}, e.g.
# "Mon 0/20". The morning turn only quotes that line. No model is in the loop
# here; a record that cannot be read writes "<weekday> count unreadable", never
# 0, and exits non-zero so the failure alert fires. The job prints NO_REPLY, so
# nothing is sent to the chat.
#
# Fail closed: after converging, the live row is re-read and the script exits 1
# (naming the field) unless the schedule, agent, command argv and delivery are
# the declared ones.
#
# Run on the VPS host as lifekit, from the repo checkout:
#   /srv/lifekit-stack/scripts/ensure-learnclaw-weekcount.sh
#
# Env overrides:
#   LEARNCLAW_WEEKCOUNT_CRON  cron expression (default: "10 7 * * 1,2,3,4,5,6")
#   LEARNCLAW_WEEKCOUNT_TZ    IANA tz (default: Europe/Dublin)
#   LEARNCLAW_WEEKLY_TARGET   problems to solve by hand per week, Monday to Sunday
#                             (default: 20, set by the owner for the week of
#                             2026-11-23; the one place the target lives)
#   LEARNCLAW_WEEKCOUNT_CHAT  owner Telegram chat id for the failure alert
#                             (default: LIFEKIT_TELEGRAM_CHAT from ENV_FILE)
#   ENV_FILE                  host env file (default: /srv/lifekit-secrets/stack.env)
set -euo pipefail

# shellcheck source=scripts/lib/cron-check.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/cron-check.sh"
cron_check_args "$@"

JOB_NAME=learnclaw-weekcount
WEEKLY_TARGET="${LEARNCLAW_WEEKLY_TARGET:-20}"
COUNT_FILE=/home/node/.openclaw/agents/learnclaw/workspace/weekly-count.json
AGENT=learnclaw
ACCOUNT=reading
CRON_EXPR="${LEARNCLAW_WEEKCOUNT_CRON:-10 7 * * 1,2,3,4,5,6}"
CRON_TZ="${LEARNCLAW_WEEKCOUNT_TZ:-Europe/Dublin}"
ENV_FILE="${ENV_FILE:-/srv/lifekit-secrets/stack.env}"

CHAT="${LEARNCLAW_WEEKCOUNT_CHAT:-}"
if [ -z "$CHAT" ] && [ -r "$ENV_FILE" ]; then
  CHAT="$(sed -nE 's/^[[:space:]]*LIFEKIT_TELEGRAM_CHAT=["'"'"']?([0-9-]+)["'"'"']?[[:space:]]*$/\1/p' \
    "$ENV_FILE" | head -1)"
fi
if [ -z "$CHAT" ]; then
  cron_check_fail "no owner chat id: set LEARNCLAW_WEEKCOUNT_CHAT, or LIFEKIT_TELEGRAM_CHAT in $ENV_FILE."
fi

# Refuse a schedule that can fire on Sunday (the morning message never does).
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

case "$WEEKLY_TARGET" in
  '' | *[!0-9]* | 0*) echo "LEARNCLAW_WEEKLY_TARGET must be a positive whole number, got '$WEEKLY_TARGET'." >&2; exit 1 ;;
esac

GATEWAY="$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway | head -n 1)"
[ -n "$GATEWAY" ] || cron_check_fail "no running openclaw-gateway container in compose project openclaw"

DAILY=/home/node/.openclaw/agents/learnclaw/workspace/skills/leetcode-daily/daily.py
ARGV="$(python3 -c 'import json, sys; print(json.dumps(["python3", sys.argv[1], "weekfile", "--out", sys.argv[2], "--target", sys.argv[3]]))' "$DAILY" "$COUNT_FILE" "$WEEKLY_TARGET")"

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

job_id="$(live_jobs | find_job_id "$JOB_NAME")" || cron_check_fail "could not read the automation list from $GATEWAY"

if [ -z "$job_id" ]; then
  cron_check_absent "$JOB_NAME"
  docker exec "$GATEWAY" openclaw cron add \
    --name "$JOB_NAME" \
    --display-name "Learnclaw weekly count file" \
    --description "Writes the weekly LeetCode count line (weekly-count.json in learnclaw's workspace) at 07:10 Monday to Saturday for learnclaw-morning to quote; a command payload, so an unreadable record fails the run; sends nothing. Declared by scripts/ensure-learnclaw-weekcount.sh (source: lifekit-stack)." \
    --cron "$CRON_EXPR" --tz "$CRON_TZ" --exact \
    --agent "$AGENT" --session isolated \
    --command-argv "$ARGV" \
    --timeout-seconds 60 --no-output-timeout-seconds 30 --output-max-bytes 1000 \
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
    --command-argv "$ARGV" \
    --timeout-seconds 60 --no-output-timeout-seconds 30 --output-max-bytes 1000 \
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
job_id, agent, account, chat, argv, expr, tz = sys.argv[1:8]
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
if payload.get("kind") != "command":
    bad.append("payload.kind=" + repr(payload.get("kind")) + ", want command")
if payload.get("argv") != json.loads(argv):
    bad.append("payload.argv=" + repr(payload.get("argv")) + ", want " + argv)
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
    sys.exit("refusing: learnclaw-weekcount is not the declared row: " + "; ".join(bad))
' "$job_id" "$AGENT" "$ACCOUNT" "$CHAT" "$ARGV" "$CRON_EXPR" "$CRON_TZ" >&2
cron_check_agrees "$JOB_NAME" "$job_id"

echo "cron '$JOB_NAME' ($job_id) converged: $CRON_EXPR @ $CRON_TZ, agent:$AGENT isolated, command payload (no model turn), announce -> telegram:$ACCOUNT (verified), failure alert after 1 via telegram:default"
echo "verify: docker exec $GATEWAY openclaw cron show $job_id"
echo "        docker exec $GATEWAY python3 $DAILY weekfile --out /tmp/w.json --target $WEEKLY_TARGET && cat /tmp/w.json"

# Record the installed commit (issue 689; the release gauge reads it).
bash "$(dirname "${BASH_SOURCE[0]}")/lib/install-stamp.sh" learnclaw-weekcount
