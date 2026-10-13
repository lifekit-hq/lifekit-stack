#!/usr/bin/env bash
# ensure-today-brief-fleet.sh — idempotently add the fleet step to kit's
# `today-brief` cron row (08:30, Telegram) payload.message: run the
# secondmate-relay skill's `relay.sh status` and carry the fleet into the brief
# in the skill's Status format (calls, then to-dos, then a Later and a Fleet
# line), with the Google sign-in rule.
#
# Why a script and not config: the row is host state, created by hand with
# kit (cron rows live in the gateway's SQLite store, not in git), and its
# prompt is the owner's own text. So this script does not own the prompt, as
# ensure-finance-pulse.sh owns the pulse checklist. It inserts one step and one
# sentence at fixed anchors, and refuses (exit 1, row untouched) when an anchor
# is missing, because a prompt that changed under it needs a human to place the
# step. A prompt that already carries this version of the step is left alone,
# so re-running converges; one that carries the exact text of the previous
# version of the step (OLD_STEP, OLD_FLEET below) is upgraded in place.
#
# Run on the VPS host as lifekit, from the repo checkout, after
# ensure-secondmate-relay.sh has installed the skill:
#   /srv/lifekit-stack/scripts/ensure-today-brief-fleet.sh
#
# `openclaw cron edit --message` hot-reloads; nothing is restarted, and the
# row's schedule, delivery and tools are not touched.
set -euo pipefail

# The gateway container of the `openclaw` compose project, found by its
# compose labels rather than a fixed container name.
GATEWAY="$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway | head -n 1)"
[ -n "$GATEWAY" ] || { echo "no running openclaw-gateway container in compose project openclaw" >&2; exit 1; }
JOB_NAME=today-brief

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

# 1. Find the row by name (ids differ per box; never hardcode one).
jobs="$(docker exec "$GATEWAY" openclaw cron list --all --json 2>/dev/null | pick_json jobs)" || {
  echo "could not read the automation list from $GATEWAY" >&2; exit 1; }
job_id="$(printf '%s' "$jobs" | python3 -c '
import json, sys
for job in json.load(sys.stdin)["jobs"]:
    if job.get("name") == sys.argv[1]:
        print(job["id"])
        break' "$JOB_NAME")"
if [ -z "$job_id" ]; then
  echo "no '$JOB_NAME' automation row. It is host state, not applied by deploy." >&2
  exit 1
fi

# 2. Build the new message from the live one. Exit 10: already there, 11: an
#    anchor is missing.
status=0
# shellcheck disable=SC2016 # the single quotes pass the python program verbatim; the backticks are literal
wanted="$(printf '%s' "$jobs" | python3 -c '
import json, sys

MARK = "secondmate-relay/relay.sh status"
STATUS = "sh /home/node/.openclaw/agents/kit/workspace/skills/secondmate-relay/relay.sh status"
STEP = (
    "6. Fleet: run `" + STATUS + "` (read-only; send no relay note, never ask_user). "
    "From the JSON keep only the decisions_open items whose hold_bucket is live or absent. "
    "Calls first: \"Your call (<n>)\", then numbered lines \"<k>. <what is being decided, in plain words>\", "
    "the options from the reason of the item on the line below (lettered A, B, C in the order shown, recommended "
    "first) only when the reason names options, then on an item with options the line \"↳ reply \"<k> A\"\" "
    "with its own number and its first letter, and the board_url when set. Then \"Your to-dos (<n>)\", one line "
    "\"· <verb first>\" for each kept item that is his own action; when unsure, an item is a call. Never print "
    "a home_id or an id. Notes, each "
    "only when it applies: \"Fleet view is partial: last updated <age> ago.\" when generated_epoch is more than "
    "30 minutes old (never repeat a reason, never say invalid); "
    "\"Later: <n> parked · <n> dated · <n> blocked\" counting the queued items whose hold_bucket is aged, dated "
    "and blocked (leave out zero parts); \"Fleet: <counts.active_children> working · <counts.queued_open> queued "
    "· <n> landed today\" where <n> counts the landed items whose completion.date is today; \"+<N> not shown\" "
    "only when counts.decisions_open is larger than the decisions_open list. If the command fails, keep one line "
    "\"Fleet status unavailable: <stderr line>.\" Never call start_google_auth and never show an auth URL: when "
    "Google calendar or mail fails with an expired or revoked sign-in, say once \"Google sign-in expired - mail "
    "and calendar skipped\" and add the to-do \"Redo the Google sign-in on the PC (docs/google-mcp-setup.md, "
    "steps 6-7)\"."
)
BEFORE = "\n\nMessage (Telegram"
EMPTY = "If calendar, mail, deadlines-within-7-days and non-bot PRs are all empty, reply exactly NO_REPLY."
FLEET = (
    "After PRs waiting, add the Fleet section from step 6: the Your call and Your to-dos lists first, then the "
    "notes (this section is outside the 10-line limit). When the step 6 command succeeded and found no call "
    "and no to-do it is the single line \"Nothing needs you today.\", never left out; when it failed, only "
    "its \"Fleet status unavailable\" line."
)
# The text an earlier version of this script inserted. A row that carries
# exactly it is upgraded in place; any other prompt with a fleet step is not
# ours to rewrite.
OLD_STEP = (
    "6. Fleet: run `" + STATUS + "` "
    "(read-only; send no relay note, never ask_user). Keep: the line \"Status is stale: generated <age> ago.\" "
    "when generated_epoch is more than 5 minutes old; one line \"Status is invalid for <home id>: <reason>.\" "
    "per homes[] entry whose valid is false; counts.active_children, counts.queued and the number of landed "
    "items whose completion.date is today; each decisions_open item as \"<home_id> / <id> - <summary>\" "
    "(and how many are not shown when counts.decisions_open exceeds the list). If the command fails, keep one "
    "line \"Fleet status unavailable: <stderr line>.\""
)
OLD_FLEET = (
    "After PRs waiting, add the Fleet section from step 6 when it has anything to say: warnings first, then "
    "\"Fleet: N active, N queued, N landed today\", then the open decisions, one per line (this section is "
    "outside the 10-line limit). If calendar, mail, deadlines-within-7-days and non-bot PRs are all empty and "
    "step 6 found no open decisions and no warning, reply exactly NO_REPLY."
)
msg = ""
for job in json.load(sys.stdin)["jobs"]:
    if job.get("name") == sys.argv[1]:
        msg = (job.get("payload") or {}).get("message") or ""
        break
if STEP in msg and FLEET in msg:
    sys.exit(10)
if OLD_STEP in msg and OLD_FLEET in msg:
    sys.stdout.write(msg.replace(OLD_STEP, STEP, 1).replace(OLD_FLEET, FLEET, 1))
    sys.exit(0)
if MARK in msg or msg.count(BEFORE) != 1 or msg.count(EMPTY) != 1:
    sys.exit(11)
sys.stdout.write(msg.replace(BEFORE, "\n" + STEP + BEFORE, 1).replace(EMPTY, FLEET, 1))
' "$JOB_NAME")" || status=$?
case "$status" in
  0) ;;
  10) echo "payload.message of '$JOB_NAME' ($job_id) already carries the fleet step; nothing to do"; exit 0 ;;
  11)
    echo "payload.message of '$JOB_NAME' ($job_id) no longer has the text this script anchors on; row left untouched." >&2
    echo "Add the step by hand (scripts/ensure-today-brief-fleet.sh holds its text)." >&2
    exit 1
    ;;
  *) echo "could not build the new message (python exit $status)" >&2; exit 1 ;;
esac

# 3. Set it.
docker exec "$GATEWAY" openclaw cron edit "$job_id" --message "$wanted" >/dev/null
echo "payload.message of '$JOB_NAME' ($job_id) now carries the fleet step"
echo "verify: docker exec $GATEWAY openclaw cron show $job_id"
echo "        docker exec $GATEWAY openclaw cron run $job_id --expect-final --json   # sends the brief to Telegram"
