#!/usr/bin/env bash
# ensure-leetcode-daily.sh — idempotently declare learnclaw's daily LeetCode
# problem: one scheduled agent turn at the home-day 17:30 interview-problem slot
# that sends the owner one problem, name and link only, in learnclaw's own
# Telegram chat (account `reading`, never Kit's `default`).
#
# The job is a command payload, not an agent turn: it runs
# `daily.py next --record --push` directly (as `workout-nudge` does), with no
# model in the loop, so a daily.py error (vault page missing, lint refusing the
# row, unreadable list) exits non-zero and the scheduler counts the run as
# failed. The script decides the day (Tuesday and Thursday are office days,
# Sunday is rest: it prints NO_REPLY, which the scheduler suppresses, so those
# days send nothing even if the schedule were ever widened), takes the first
# problem in the curated list (skills/leetcode-daily/problems.json) that has no
# row in the artefact record, and appends the offered row. A problem that is
# done, skipped or offered never comes back. Its stdout, the problem name and
# link, is announced as it is. The payload carries no state: the day and the
# record are read by the script at run time (docs/runbook.md, "The payload is
# stateless").
#
# Why a script and not config: cron jobs live in the gateway's SQLite state,
# not in openclaw.json — same split as the other ensure-*.sh scripts. This
# script IS the git-side declaration of the job.
#
# Fail closed: after converging, the live row is re-read and the script exits 1
# (naming the field) unless the schedule only fires on home days, the agent is
# learnclaw, the payload is a command with exactly the declared argv, and
# delivery is announce to Telegram account `reading` at the owner chat. A row that could deliver through Kit's account fails the run instead of
# pinging the owner from the wrong bot.
#
# Failure: a run that fails outright is counted by the scheduler and the failure
# alert goes through kit's `default` Telegram account to the owner chat, the
# route the digest and the pulse use. A daily.py error is such a failure: the
# command exits non-zero, no message reaches learnclaw's chat, and the alert
# fires. The owner chat id is read from the host env file and never written to
# git.
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

# shellcheck source=scripts/lib/cron-check.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/cron-check.sh"
cron_check_args "$@"

JOB_NAME=leetcode-daily
AGENT=learnclaw
ACCOUNT=reading
CRON_EXPR="${LEETCODE_DAILY_CRON:-30 17 * * 1,3,5,6}"
CRON_TZ="${LEETCODE_DAILY_TZ:-Europe/Dublin}"
ENV_FILE="${ENV_FILE:-/srv/lifekit-secrets/stack.env}"

CHAT="${LEETCODE_DAILY_CHAT:-}"
if [ -z "$CHAT" ] && [ -r "$ENV_FILE" ]; then
  CHAT="$(sed -nE 's/^[[:space:]]*LIFEKIT_TELEGRAM_CHAT=["'"'"']?([0-9-]+)["'"'"']?[[:space:]]*$/\1/p' \
    "$ENV_FILE" | head -1)"
fi
if [ -z "$CHAT" ]; then
  cron_check_fail "no owner chat id: set LEETCODE_DAILY_CHAT, or LIFEKIT_TELEGRAM_CHAT in $ENV_FILE."
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
[ -n "$GATEWAY" ] || cron_check_fail "no running openclaw-gateway container in compose project openclaw"

DAILY=/home/node/.openclaw/agents/learnclaw/workspace/skills/leetcode-daily/daily.py
ARGV="$(python3 -c 'import json, sys; print(json.dumps(["python3", sys.argv[1], "next", "--record", "--push"]))' "$DAILY")"

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

# A row that is still the old agent turn cannot fail itself, so it is removed
# and declared again as a command payload (a run history of the old turns goes
# with it; the record in the vault is untouched). --check only reports such a
# row, through the verify below.
if [ -n "$job_id" ] && [ "$CRON_CHECK" = 0 ]; then
  kind="$(live_jobs | python3 -c '
import json, sys
job = next(j for j in json.load(sys.stdin)["jobs"] if j.get("id") == sys.argv[1])
print((job.get("payload") or {}).get("kind", ""))' "$job_id")" || exit 1
  if [ "$kind" != command ]; then
    docker exec "$GATEWAY" openclaw cron rm "$job_id" >/dev/null
    echo "removed the '$JOB_NAME' agent-turn row ($job_id); declaring it as a command"
    job_id=""
  fi
fi

if [ -z "$job_id" ]; then
  cron_check_absent "$JOB_NAME"
  docker exec "$GATEWAY" openclaw cron add \
    --name "$JOB_NAME" \
    --display-name "Daily LeetCode problem" \
    --description "One LeetCode problem (name and link) on home days at 17:30, in learnclaw's own Telegram chat; a command payload, so a daily.py error fails the run; picked and recorded by skills/leetcode-daily. Declared by scripts/ensure-leetcode-daily.sh (source: lifekit-stack)." \
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
job_id, agent, account, chat, argv, expr = sys.argv[1:7]
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
' "$job_id" "$AGENT" "$ACCOUNT" "$CHAT" "$ARGV" "$CRON_EXPR" >&2
cron_check_agrees "$JOB_NAME" "$job_id"

echo "cron '$JOB_NAME' ($job_id) converged: $CRON_EXPR @ $CRON_TZ, agent:$AGENT isolated, command payload (no model turn), announce -> telegram:$ACCOUNT (verified), failure alert after 1 via telegram:default"
echo "verify: docker exec $GATEWAY openclaw cron show $job_id"
echo "        docker exec $GATEWAY python3 $DAILY next --push --date <a Tuesday>   # NO_REPLY on an office day (Tuesday), nothing recorded"
echo "        docker exec $GATEWAY python3 $DAILY status"
