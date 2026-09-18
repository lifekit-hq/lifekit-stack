#!/usr/bin/env bash
# ensure-finance-pulse.sh — idempotently declare the finance agent's pulse
# checklist (the heartbeat's monitor scratch).
#
# Why a script and not config: the heartbeat's CADENCE is config
# (agents.entries.finance.heartbeat in compose/openclaw-gateway/
# platform.patch.json, applied by deploy.sh), but its CHECKLIST is not. OpenClaw
# 2026.9.4 retired the workspace HEARTBEAT.md (accepted, no-op); the checklist
# is the per-automation scratch of the `heartbeat-<agent>` monitor row, which
# lives in the gateway's SQLite state, not in openclaw.json. Same split as
# ensure-morning-brief.sh: this script IS the git-side declaration, and
# scripts/finance-pulse.md is the text it declares.
#
# The cost contract this preserves: a heartbeat whose scratch is effectively
# empty skips the run (reason=empty-heartbeat-file) with no model call. So
# until this script has run, the pulse costs nothing; after it, the model runs
# on the cadence in the patch file and replies NO_REPLY when nothing changed.
#
# Run on the VPS host as lifekit, from the repo checkout, AFTER the deploy
# that applied the heartbeat block (the monitor row must exist):
#   /srv/lifekit-stack/scripts/ensure-finance-pulse.sh
#
# Converges without fighting the agent, which may rewrite its own scratch
# through heartbeat_respond {scratch}:
#   scratch empty/absent      -> set from scripts/finance-pulse.md
#   scratch equals the file   -> nothing to do
#   scratch differs           -> left untouched; re-run with
#                                FINANCE_PULSE_REPLACE=1 to overwrite it
set -euo pipefail

GATEWAY=compose-openclaw-gateway-1
AGENT=finance
JOB_NAME="heartbeat-${AGENT}"
REPLACE="${FINANCE_PULSE_REPLACE:-0}"

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

# 1. Find the monitor row by name (ids differ per box; never hardcode one).
jobs="$(docker exec "$GATEWAY" openclaw cron list --all --json 2>/dev/null | pick_json jobs)" || {
  echo "could not read the automation list from $GATEWAY" >&2; exit 1; }
row="$(printf '%s' "$jobs" | python3 -c '
import json, sys
for job in json.load(sys.stdin)["jobs"]:
    if job.get("name") == sys.argv[1]:
        print(job["id"], str(job.get("enabled", False)).lower())
        break' "$JOB_NAME")"
if [ -z "$row" ]; then
  echo "no '$JOB_NAME' automation row. The heartbeat block in" >&2
  echo "compose/openclaw-gateway/platform.patch.json has not been applied yet" >&2
  echo "(deploy first), or the row was never materialized: openclaw doctor --fix." >&2
  exit 1
fi
job_id="${row%% *}"
enabled="${row##* }"
[ "$enabled" = "true" ] || echo "note: '$JOB_NAME' ($job_id) is disabled; the checklist is set but will not tick until the heartbeat is enabled"

# 2. Compare the live scratch with the declared text.
current="$(docker exec "$GATEWAY" openclaw cron scratch "$job_id" --json 2>/dev/null \
  | pick_json currentRevision \
  | python3 -c 'import json, sys; print((json.load(sys.stdin).get("scratch") or {}).get("content") or "", end="")')" || {
  echo "could not read the scratch of '$JOB_NAME' ($job_id)" >&2; exit 1; }
wanted="$(cat "$checklist")"

if [ "$current" = "$wanted" ]; then
  echo "scratch of '$JOB_NAME' ($job_id) already matches ${checklist#"${repo_root}/"}; nothing to do"
  exit 0
fi
if [ -n "${current//[[:space:]]/}" ] && [ "$REPLACE" != "1" ]; then
  echo "scratch of '$JOB_NAME' ($job_id) differs from ${checklist#"${repo_root}/"}; leaving it untouched"
  echo "(the agent may have rewritten it. Inspect: docker exec $GATEWAY openclaw cron scratch $job_id"
  echo " overwrite with the git text: FINANCE_PULSE_REPLACE=1 $0)"
  exit 0
fi

# 3. Set it (stdin, so the text never has to exist inside the container).
docker exec -i "$GATEWAY" openclaw cron scratch "$job_id" --file - < "$checklist" >/dev/null
echo "scratch of '$JOB_NAME' ($job_id) set from ${checklist#"${repo_root}/"}"
echo "verify: docker exec $GATEWAY openclaw cron scratch $job_id"
echo "        docker exec $GATEWAY openclaw system heartbeat last --json   # after the next tick"
