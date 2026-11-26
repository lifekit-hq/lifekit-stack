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
# exactly one message) followed by scripts/finance-pulse.md verbatim. This
# script IS the git-side declaration, and scripts/finance-pulse.md is the
# text it declares.
#
# Run on the VPS host as lifekit, from the repo checkout, AFTER the
# `ledger-pulse` cron row has been created on the gateway (see docs/runbook.md):
#   /srv/lifekit-stack/scripts/ensure-finance-pulse.sh
#
# It also installs the row's gate (scripts/finance-pulse-gate/) into the gateway
# state dir and sets it as the row's trigger script, so a run with nothing
# pending costs no model turn (docs/runbook.md, "The ledger-pulse gate").
#
# Converges: the live payload.message, payload.timeoutSeconds, payload.model
# (the repo's cron model, scripts/lib/cron-model.sh) and trigger script
# are compared with the declared ones and rewritten with `openclaw cron edit
# --message` / `--timeout-seconds` / `--model` / `--trigger-script` only when
# they differ. The edit hot-reloads (cron rows are a no-op-restart category);
# nothing is restarted. The row's schedule, delivery and the old scratch are not
# touched.
#
# It also converges the row's failure alert (`--failure-alert ...` to the owner
# chat), so a gate fault (which throws) reaches Telegram instead of going quiet.
#
# Env overrides:
#   OPENCLAW_CONFIG_DIR  host path of the gateway state dir (default: /srv/openclaw/config, as in compose)
#   FINANCE_PULSE_ALERT_CHAT  owner Telegram chat id for the failure alert
#                        (default: LIFEKIT_TELEGRAM_CHAT from ENV_FILE)
#   ENV_FILE             host env file (default: /srv/lifekit-secrets/stack.env)
set -euo pipefail

# shellcheck source=scripts/lib/cron-check.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/cron-check.sh"
cron_check_args "$@"

ENV_FILE="${ENV_FILE:-/srv/lifekit-secrets/stack.env}"

# The run's wall-clock limit. Successful pulse runs take 100-170 s (the model
# call dominates), and a 180 s limit left ordinary variance failing runs
# (180-188 s) and firing the critical "scheduled or background run failed"
# alert before a retry succeeded. 300 s is about 1.75x the slowest observed
# successful run: real headroom, still short of letting a wedged run hang.
TIMEOUT_SECONDS=300

# The gateway container of the `openclaw` compose project, found by its
# compose labels rather than a fixed container name.
GATEWAY="$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway | head -n 1)"
[ -n "$GATEWAY" ] || cron_check_fail "no running openclaw-gateway container in compose project openclaw"
JOB_NAME="ledger-pulse"
# The one cron model, from the repo's config (scripts/lib/cron-model.sh).
# shellcheck source=scripts/lib/cron-model.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/cron-model.sh"
MODEL="${FINANCE_PULSE_MODEL:-$(cron_model)}"

ALERT_CHAT="${FINANCE_PULSE_ALERT_CHAT:-}"
if [ -z "$ALERT_CHAT" ] && [ -r "$ENV_FILE" ]; then
  ALERT_CHAT="$(sed -nE 's/^[[:space:]]*LIFEKIT_TELEGRAM_CHAT=["'"'"']?([0-9-]+)["'"'"']?[[:space:]]*$/\1/p' \
    "$ENV_FILE" | head -1)"
fi
[ -n "$ALERT_CHAT" ] || cron_check_fail "no owner chat id for the failure alert: set FINANCE_PULSE_ALERT_CHAT or LIFEKIT_TELEGRAM_CHAT in $ENV_FILE"

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
checklist="${repo_root}/scripts/finance-pulse.md"
[ -s "$checklist" ] || { echo "missing or empty: $checklist" >&2; exit 1; }
gate_src="${repo_root}/scripts/finance-pulse-gate"
gate_dest="${OPENCLAW_CONFIG_DIR:-/srv/openclaw/config}/gates/ledger-pulse"
# The same path inside the gateway container (the state dir is bind-mounted).
gate_script="/home/node/.openclaw/gates/ledger-pulse/gate.js"
{ [ -s "$gate_src/gate.js" ] && [ -s "$gate_src/pulse_gate.py" ]; } \
  || { echo "missing or empty: $gate_src/gate.js or pulse_gate.py" >&2; exit 1; }

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
  cron_check_fail "could not read the automation list from $GATEWAY"; }
row="$(printf '%s' "$jobs" | python3 -c '
import json, sys
for job in json.load(sys.stdin)["jobs"]:
    if job.get("name") == sys.argv[1]:
        print(job["id"], str(job.get("enabled", False)).lower())
        break' "$JOB_NAME")"
if [ -z "$row" ]; then
  cron_check_absent "$JOB_NAME"
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

# 3. Compare with the live payload.message, payload.timeoutSeconds and trigger script.
current="$(printf '%s' "$jobs" | python3 -c '
import json, sys
for job in json.load(sys.stdin)["jobs"]:
    if job.get("name") == sys.argv[1]:
        print((job.get("payload") or {}).get("message") or "", end="")
        break' "$JOB_NAME")"
current_timeout="$(printf '%s' "$jobs" | python3 -c '
import json, sys
for job in json.load(sys.stdin)["jobs"]:
    if job.get("name") == sys.argv[1]:
        print((job.get("payload") or {}).get("timeoutSeconds", ""), end="")
        break' "$JOB_NAME")"

current_model="$(printf '%s' "$jobs" | python3 -c '
import json, sys
for job in json.load(sys.stdin)["jobs"]:
    if job.get("name") == sys.argv[1]:
        print((job.get("payload") or {}).get("model") or "", end="")
        break' "$JOB_NAME")"
current_gate="$(printf '%s' "$jobs" | python3 -c '
import json, sys
for job in json.load(sys.stdin)["jobs"]:
    if job.get("name") == sys.argv[1]:
        print(((job.get("trigger") or {}).get("script") or "").strip(), end="")
        break' "$JOB_NAME")"
wanted_gate="$(cat "$gate_src/gate.js")"
current_alert_to="$(printf '%s' "$jobs" | python3 -c '
import json, sys
for job in json.load(sys.stdin)["jobs"]:
    if job.get("name") == sys.argv[1]:
        print(str((job.get("failureAlert") or {}).get("to") or ""), end="")
        break' "$JOB_NAME")"

if [ "$CRON_CHECK" = 0 ]; then
  install -D -m 0644 "$gate_src/gate.js" "$gate_dest/gate.js"
  install -D -m 0755 "$gate_src/pulse_gate.py" "$gate_dest/pulse_gate.py"
fi

edit_args=()
[ "$current_gate" = "$wanted_gate" ] || edit_args+=(--trigger-script "$gate_script")
[ "$current" = "$wanted" ] || edit_args+=(--message "$wanted")
[ "$current_timeout" = "$TIMEOUT_SECONDS" ] || edit_args+=(--timeout-seconds "$TIMEOUT_SECONDS")
# model@anthropic:api-key is the same model with its inference account recorded.
[ "$current_model" = "$MODEL" ] || [ "$current_model" = "${MODEL}@anthropic:api-key" ] \
  || edit_args+=(--model "$MODEL")
[ "$current_alert_to" = "$ALERT_CHAT" ] || edit_args+=(--failure-alert --failure-alert-after 1
  --failure-alert-mode announce --failure-alert-channel telegram
  --failure-alert-account-id default --failure-alert-to "$ALERT_CHAT")

# --check: every field this script owns must already be the declared value.
# (Whether the row is enabled is the operator's: a disabled pulse is a note above.)
if [ "$CRON_CHECK" = 1 ]; then
  [ "${#edit_args[@]}" -eq 0 ] || {
    drift=""
    [ "$current_gate" = "$wanted_gate" ] || drift+=" trigger"
    [ "$current" = "$wanted" ] || drift+=" payload.message"
    [ "$current_timeout" = "$TIMEOUT_SECONDS" ] || drift+=" payload.timeoutSeconds"
    [ "$current_model" = "$MODEL" ] || [ "$current_model" = "${MODEL}@anthropic:api-key" ] || drift+=" payload.model"
    [ "$current_alert_to" = "$ALERT_CHAT" ] || drift+=" failureAlert"
    echo "drift: cron '$JOB_NAME' ($job_id) differs from $(basename "$0") in:$drift" >&2
    exit 1
  }
  cron_check_agrees "$JOB_NAME" "$job_id"
fi

if [ "${#edit_args[@]}" -eq 0 ]; then
  echo "payload.message, timeoutSeconds ($TIMEOUT_SECONDS), model ($MODEL), gate and failure alert of '$JOB_NAME' ($job_id) already declared (${checklist#"${repo_root}/"}, ${gate_src#"${repo_root}/"}); nothing to do"
  exit 0
fi

# 4. Set what differs.
docker exec "$GATEWAY" openclaw cron edit "$job_id" "${edit_args[@]}" >/dev/null
[ "$current" = "$wanted" ] || echo "payload.message of '$JOB_NAME' ($job_id) set from ${checklist#"${repo_root}/"}"
[ "$current_timeout" = "$TIMEOUT_SECONDS" ] || echo "payload.timeoutSeconds of '$JOB_NAME' ($job_id) set to $TIMEOUT_SECONDS (was ${current_timeout:-unset})"
[ "$current_model" = "$MODEL" ] || [ "$current_model" = "${MODEL}@anthropic:api-key" ] || echo "payload.model of '$JOB_NAME' ($job_id) set to $MODEL (was ${current_model:-unset})"
[ "$current_gate" = "$wanted_gate" ] || echo "trigger script of '$JOB_NAME' ($job_id) set to $gate_script"
[ "$current_alert_to" = "$ALERT_CHAT" ] || echo "failure alert of '$JOB_NAME' ($job_id) set to announce on telegram"
echo "verify: docker exec $GATEWAY openclaw cron show $job_id"
echo "        docker exec $GATEWAY openclaw cron run $job_id --expect-final --json   # trigger a run"
