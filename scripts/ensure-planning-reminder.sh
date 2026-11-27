#!/usr/bin/env bash
# ensure-planning-reminder.sh — idempotently declare Kit's weekly planning
# reminder (issue 619): one scheduled message every Monday at 10:00
# Europe/Dublin, to the owner in Kit's own Telegram chat (account `default`),
# pointing at the weekly planning session and nothing else.
#
# A command payload, not an agent turn: the text is fixed, so no model runs,
# nothing is read (no vault, no mail, no calendar) and nothing private can end
# up in the message. The row's argv is `echo <text>`; announce delivery sends
# its output. The text carries no secrets and no personal data, and the owner
# chat id is read from the host env file, never written to git.
#
# Why a script and not config: cron jobs live in the gateway's SQLite state,
# not in openclaw.json — same split as the other ensure-*.sh scripts. This
# script IS the git-side declaration of the job.
#
# Fail closed: it refuses (exit 1, plain reason) when the owner chat id cannot
# be resolved, rather than guessing a target, and when the schedule is not
# Monday only. After converging, the live row is re-read and the script exits
# 1 (naming the field) unless the agent, payload, schedule, timezone and
# delivery (announce to Telegram `default` at the owner chat) are the declared
# ones. A re-run on a converged row changes nothing.
#
# Run on the VPS host as lifekit, from the repo checkout (docs/runbook.md,
# "Weekly planning reminder"):
#   /srv/lifekit-stack/scripts/ensure-planning-reminder.sh
#
# Env overrides:
#   PLANNING_REMINDER_CRON  cron expression (default: "0 10 * * 1"; the check
#                           below refuses any that can fire on another day)
#   PLANNING_REMINDER_TZ    IANA tz (default: Europe/Dublin)
#   PLANNING_REMINDER_CHAT  owner Telegram chat id for delivery and the failure
#                           alert (default: LIFEKIT_TELEGRAM_CHAT from ENV_FILE)
#   ENV_FILE                host env file (default: /srv/lifekit-secrets/stack.env)
set -euo pipefail
# The production gateway container, never a second labelled one (issue 716).
# shellcheck source=scripts/lib/openclaw-gateway.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/openclaw-gateway.sh"

# shellcheck source=scripts/lib/cron-check.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/cron-check.sh"
cron_check_args "$@"

JOB_NAME=planning-reminder
AGENT=kit
ACCOUNT=default
CRON_EXPR="${PLANNING_REMINDER_CRON:-0 10 * * 1}"
CRON_TZ="${PLANNING_REMINDER_TZ:-Europe/Dublin}"
ENV_FILE="${ENV_FILE:-/srv/lifekit-secrets/stack.env}"
TEXT="Monday planning session: time to open this week's planning session."

CHAT="${PLANNING_REMINDER_CHAT:-}"
if [ -z "$CHAT" ] && [ -r "$ENV_FILE" ]; then
  CHAT="$(sed -nE 's/^[[:space:]]*LIFEKIT_TELEGRAM_CHAT=["'"'"']?([0-9-]+)["'"'"']?[[:space:]]*$/\1/p' \
    "$ENV_FILE" | head -1)"
fi
if [ -z "$CHAT" ]; then
  cron_check_fail "no owner chat id: set PLANNING_REMINDER_CHAT, or LIFEKIT_TELEGRAM_CHAT in $ENV_FILE."
fi

# Refuse a schedule that can fire on any day but Monday, before touching the
# gateway. Field 5 of the expression must be exactly 1.
python3 -I - "$CRON_EXPR" <<'PY'
import sys

fields = sys.argv[1].split()
if len(fields) != 5:
    sys.exit(f"refusing: cron {sys.argv[1]!r} is not a five-field expression")
if fields[2] != "*" or fields[3] != "*" or fields[4] != "1":
    sys.exit(f"refusing: cron {sys.argv[1]!r} is not weekly on Monday only")
PY

GATEWAY="$(openclaw_gateway_id)" || cron_check_fail "cannot pick the openclaw-gateway container"
[ -n "$GATEWAY" ] || cron_check_fail "no running openclaw-gateway container in compose project openclaw"

ARGV="$(python3 -c 'import json, sys; print(json.dumps(["echo", sys.argv[1]]))' "$TEXT")"

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
    --display-name "Weekly planning reminder" \
    --description "A fixed one-line reminder of the weekly planning session, Mondays at 10:00, in Kit's Telegram chat. A command payload: no model turn, nothing read. Declared by scripts/ensure-planning-reminder.sh (source: lifekit-stack)." \
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
    sys.exit("refusing: planning-reminder is not the declared row: " + "; ".join(bad))
' "$job_id" "$AGENT" "$ACCOUNT" "$CHAT" "$ARGV" "$CRON_EXPR" "$CRON_TZ" >&2
cron_check_agrees "$JOB_NAME" "$job_id"

echo "cron '$JOB_NAME' ($job_id) converged: $CRON_EXPR @ $CRON_TZ, agent:$AGENT isolated, command payload (no model turn), announce -> telegram:$ACCOUNT (verified), failure alert after 1 via telegram:default"
echo "verify: docker exec $GATEWAY openclaw cron show $job_id"
echo "test:   docker exec $GATEWAY openclaw cron run $job_id --expect-final --json   # one message in Kit's chat"

# Record the installed commit (issue 689; the release gauge reads it).
bash "$(dirname "${BASH_SOURCE[0]}")/lib/install-stamp.sh" planning-reminder
