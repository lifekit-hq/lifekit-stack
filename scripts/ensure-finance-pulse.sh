#!/usr/bin/env bash
# ensure-finance-pulse.sh — idempotently declare the finance agent's pulse
# checklist in the `ledger-pulse` cron row's payload.message.
#
# Why a script and not config: the pulse's CADENCE is host state - the
# `ledger-pulse` cron row (docs/runbook.md, "The finance heartbeat, retired
# for an isolated cron job"), created by hand on the gateway CLI, not in
# git - but its CHECKLIST doesn't have to be. The row is an `agentTurn` job,
# and an agentTurn run receives only its payload.message: the per-job scratch
# (what a heartbeat tick reads) never reaches it, so a checklist seeded there
# is silently never shown to the model. This script therefore writes the
# checklist into payload.message: a short contract preamble (NO_REPLY or
# exactly one message) followed by scripts/finance-pulse.md verbatim. Same
# split as ensure-morning-brief.sh: this script IS the git-side declaration,
# and scripts/finance-pulse.md is the text it declares.
#
# Run on the VPS host as lifekit, from the repo checkout, AFTER the
# `ledger-pulse` cron row has been created on the gateway (see docs/runbook.md):
#   /srv/lifekit-stack/scripts/ensure-finance-pulse.sh
#
# Converges: the live payload.message is compared with the declared one and
# rewritten with `openclaw cron edit --message` only when it differs. The
# edit hot-reloads (cron rows are a no-op-restart category); nothing is
# restarted. The row's schedule, delivery, model and the old scratch are not
# touched.
set -euo pipefail

# The gateway container of the `openclaw` compose project, found by its
# compose labels rather than a fixed container name.
GATEWAY="$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway | head -n 1)"
[ -n "$GATEWAY" ] || { echo "no running openclaw-gateway container in compose project openclaw" >&2; exit 1; }
JOB_NAME="ledger-pulse"

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
checklist="${repo_root}/scripts/finance-pulse.md"
[ -s "$checklist" ] || { echo "missing or empty: $checklist" >&2; exit 1; }

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

# 1. Find the cron row by name (ids differ per box; never hardcode one).
jobs="$(docker exec "$GATEWAY" openclaw cron list --all --json 2>/dev/null | pick_json jobs)" || {
  echo "could not read the automation list from $GATEWAY" >&2; exit 1; }
row="$(printf '%s' "$jobs" | python3 -c '
import json, sys
for job in json.load(sys.stdin)["jobs"]:
    if job.get("name") == sys.argv[1]:
        print(job["id"], str(job.get("enabled", False)).lower())
        break' "$JOB_NAME")"
if [ -z "$row" ]; then
  echo "no '$JOB_NAME' automation row. It is host state, not applied by deploy:" >&2
  echo "create it by hand on the gateway CLI (docs/runbook.md, \"The finance" >&2
  echo "heartbeat, retired for an isolated cron job\")." >&2
  exit 1
fi
job_id="${row%% *}"
enabled="${row##* }"
[ "$enabled" = "true" ] || echo "note: '$JOB_NAME' ($job_id) is disabled; the checklist is set but will not tick until the cron row is enabled"

# 2. Build the declared message: contract preamble + the checklist verbatim.
preamble="Periodic check-in for the Ledger finance agent pulse. The checklist below is your whole task list for this run; do not infer or repeat tasks from prior chats. If nothing on it needs the operators attention, your entire final reply must be exactly NO_REPLY and nothing else. If something does, send exactly one Telegram message covering it, then stop -- no additional messages, working notes, or recaps."
wanted="${preamble}"$'\n\n'"$(cat "$checklist")"

# 3. Compare with the live payload.message.
current="$(printf '%s' "$jobs" | python3 -c '
import json, sys
for job in json.load(sys.stdin)["jobs"]:
    if job.get("name") == sys.argv[1]:
        print((job.get("payload") or {}).get("message") or "", end="")
        break' "$JOB_NAME")"

if [ "$current" = "$wanted" ]; then
  echo "payload.message of '$JOB_NAME' ($job_id) already carries ${checklist#"${repo_root}/"}; nothing to do"
  exit 0
fi

# 4. Set it.
docker exec "$GATEWAY" openclaw cron edit "$job_id" --message "$wanted" >/dev/null
echo "payload.message of '$JOB_NAME' ($job_id) set from ${checklist#"${repo_root}/"}"
echo "verify: docker exec $GATEWAY openclaw cron show $job_id"
echo "        docker exec $GATEWAY openclaw cron run $job_id --expect-final --json   # trigger a run"
