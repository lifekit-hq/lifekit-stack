#!/usr/bin/env bash
# ensure-needs-you-digest.sh — idempotently declare kit's daily needs-you
# digest: one scheduled agent turn that posts the decisions still open in
# kit's ledger (needs-you/open.json, defined in kit's AGENTS.md, "Needs you")
# into kit's main Control UI / WebChat chat, and stays silent when none are.
#
# Why a script and not config: cron jobs live in the gateway's SQLite state,
# not in openclaw.json — same split as the other ensure-*.sh scripts. This
# script IS the git-side declaration of the job.
#
# Why `--session current --session-key agent:kit:main`: a current-session job
# with announce delivery commits its final reply into the bound conversation's
# transcript, and WebChat gets it as a live session.message. kit's main
# session has no external channel route, so the commit is the whole delivery
# and nothing goes to Telegram (Telegram DMs are separate sessions under
# dmScope per-channel-peer). Without a session key, a CLI-created `current`
# job falls back to `isolated`, which has no conversation to post into.
# `--session main` is no alternative: main jobs take only system events and
# reject announce delivery. Do not set --channel webchat; announce with the
# default channel ("last") is what resolves to the conversation commit.
#
# Silence: the prompt replies NO_REPLY when nothing is open. That is the
# runner's intentional-silence token, so a quiet day posts nothing.
#
# Failure: a run that cannot read the ledger says so in the chat. A run that
# fails outright (model, provider, timeout, a failed transcript commit) is
# counted by the scheduler, and the per-job failure alert below tells the
# owner after the first failure. The alert cannot use the job's own route: a
# failure alert needs an outbound channel, and the Control UI chat is not one.
# So it goes through kit's `default` Telegram account to the owner chat, the
# same route the career weekly cron uses for its failure alert
# (docs/runbook.md). The owner chat id is read from the host env file and
# never written to git.
#
# Run on the VPS host as lifekit, from the repo checkout:
#   /srv/lifekit-stack/scripts/ensure-needs-you-digest.sh
#
# Converges on every run: creates the job when absent, then (new or existing)
# re-applies the declared schedule, session binding, prompt, delivery and
# failure alert with `cron edit`, so the live row always matches this file.
#
# Env overrides:
#   NEEDS_YOU_DIGEST_CRON  cron expression (default: "0 9 * * *")
#   NEEDS_YOU_DIGEST_TZ    IANA tz (default: Europe/Dublin — house tz of the
#                          existing jobs)
#   NEEDS_YOU_ALERT_CHAT   owner Telegram chat id for the failure alert
#                          (default: LIFEKIT_TELEGRAM_CHAT from ENV_FILE)
#   ENV_FILE               host env file (default: /srv/lifekit-secrets/stack.env)
set -euo pipefail

# The gateway container of the `openclaw` compose project, found by its
# compose labels rather than a fixed container name.
GATEWAY="$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway | head -n 1)"
[ -n "$GATEWAY" ] || { echo "no running openclaw-gateway container in compose project openclaw" >&2; exit 1; }
JOB_NAME=needs-you-digest
AGENT=kit
SESSION_KEY="agent:${AGENT}:main"
CRON_EXPR="${NEEDS_YOU_DIGEST_CRON:-0 9 * * *}"
CRON_TZ="${NEEDS_YOU_DIGEST_TZ:-Europe/Dublin}"
ENV_FILE="${ENV_FILE:-/srv/lifekit-secrets/stack.env}"

ALERT_CHAT="${NEEDS_YOU_ALERT_CHAT:-}"
if [ -z "$ALERT_CHAT" ] && [ -r "$ENV_FILE" ]; then
  ALERT_CHAT="$(sed -nE 's/^[[:space:]]*LIFEKIT_TELEGRAM_CHAT=["'"'"']?([0-9-]+)["'"'"']?[[:space:]]*$/\1/p' \
    "$ENV_FILE" | head -1)"
fi
if [ -z "$ALERT_CHAT" ]; then
  echo "no owner chat id for the failure alert: set NEEDS_YOU_ALERT_CHAT, or" >&2
  echo "LIFEKIT_TELEGRAM_CHAT in $ENV_FILE. Without it a failed digest would be silent." >&2
  exit 1
fi

MESSAGE="Daily needs-you digest. Read needs-you/open.json in your workspace and nothing else; do not edit it, do not call ask_user, sessions_send or message. If the file is missing or holds an empty array, reply exactly NO_REPLY. Otherwise reply with one short message: a first line '<N> decision(s) waiting on you', then one line per entry, oldest first: '<id> — <ask> (options: <options joined by ' / '>; since <opened>, from <origin>)', plus ' — <context>' when the entry has one. End with one line: 'Answer here: reply <id>: <choice>, or ask me to put one up as a card.' No preamble, no closing summary. If the file exists but cannot be read or is not a JSON array, reply with one line starting 'needs-you digest failed:' and the reason, so the owner sees it in this chat."

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

# Find the job by name (ids differ per box; never hardcode one).
find_job_id() {
  local jobs
  jobs="$(docker exec "$GATEWAY" openclaw cron list --all --json 2>/dev/null | pick_json jobs)" || {
    echo "could not read the automation list from $GATEWAY" >&2; return 1; }
  printf '%s' "$jobs" | python3 -c '
import json, sys
for job in json.load(sys.stdin)["jobs"]:
    if job.get("name") == sys.argv[1]:
        print(job["id"])
        break' "$JOB_NAME"
}

# 1. Look the job up.
job_id="$(find_job_id)"

# 2. Create it when absent. `cron add` has no failure-alert flags; step 3
#    sets the alert on new and existing jobs alike.
if [ -z "$job_id" ]; then
  docker exec "$GATEWAY" openclaw cron add \
    --name "$JOB_NAME" \
    --display-name "Needs-you digest" \
    --description "Daily digest of the decisions still open in kit's needs-you ledger, posted into kit's main Control UI chat; silent when none are. Declared by scripts/ensure-needs-you-digest.sh (source: lifekit-stack)." \
    --cron "$CRON_EXPR" --tz "$CRON_TZ" \
    --agent "$AGENT" --session current --session-key "$SESSION_KEY" \
    --message "$MESSAGE" \
    --timeout-seconds 300 \
    --announce \
    --json >/dev/null
  job_id="$(find_job_id)"
  [ -n "$job_id" ] || { echo "cron add ran but no '$JOB_NAME' job is listed" >&2; exit 1; }
  echo "created cron '$JOB_NAME' ($job_id)"
fi

# 3. Converge the declared shape and the failure alert.
docker exec "$GATEWAY" openclaw cron edit "$job_id" \
  --cron "$CRON_EXPR" --tz "$CRON_TZ" \
  --agent "$AGENT" --session current --session-key "$SESSION_KEY" \
  --message "$MESSAGE" \
  --timeout-seconds 300 \
  --announce --no-best-effort-deliver \
  --failure-alert --failure-alert-after 1 \
  --failure-alert-mode announce --failure-alert-channel telegram \
  --failure-alert-account-id default --failure-alert-to "$ALERT_CHAT" \
  >/dev/null
echo "cron '$JOB_NAME' ($job_id) converged: $CRON_EXPR @ $CRON_TZ, agent:$AGENT -> $SESSION_KEY (announce), failure alert after 1 via telegram:default"
echo "verify: docker exec $GATEWAY openclaw cron show $job_id   # delivery preview: announce -> current session"
echo "        docker exec $GATEWAY openclaw cron run $job_id --expect-final --json   # NO_REPLY while the ledger is empty"
