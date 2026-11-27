#!/usr/bin/env bash
# kit-notify.sh - "Kit will tell you": a one-shot notice from Kit to the owner
# in Telegram, with a delivery check (issue 725).
#
# The route is a one-shot OpenClaw cron job on agent `kit`:
#   openclaw cron add --agent kit --at <delay> --announce --channel telegram \
#     --delivery-target owner --keep-after-run ...
# The payload is a command (`echo <text>`), so no model turn runs and the text
# arrives exactly as passed in. `--delivery-target owner` lets the gateway
# resolve the owner's DM itself: this script never reads, passes or prints a
# chat id. The job is kept after it runs (`--keep-after-run`, never
# `--delete-after-run`) so its run record survives for the check below.
#
# Usage (on the VPS host, as lifekit; docs/runbook.md "Kit one-shot notice"):
#   kit-notify.sh send [--at <delay>] [--wait] <text>
#       Schedule the notice. Prints the job id and the fire time. With --wait,
#       wait past the fire time and run the check below (same exit codes).
#   kit-notify.sh check <job-id>
#       Read `openclaw cron runs <id> --json` and report one of:
#         delivered    exit 0; the job is then deleted
#         failed       exit 1, with the reason; the job is kept for inspection
#         not-yet-run  exit 3; run the check again after the fire time
#
# Exit 2 is a usage error; any other failure (no gateway, unreadable output)
# exits 1 with a plain reason.
#
# Env overrides:
#   KIT_NOTIFY_AT         default delay for `send` (default: 1m)
#   KIT_NOTIFY_WAIT_MAX   seconds --wait keeps polling after the fire time
#                         (default: 180)
#   KIT_NOTIFY_POLL       seconds between --wait polls (default: 10)
set -euo pipefail
# The production gateway container, never a second labelled one (issue 716).
# shellcheck source=scripts/lib/openclaw-gateway.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/openclaw-gateway.sh"

AGENT=kit
AT="${KIT_NOTIFY_AT:-1m}"
WAIT_MAX="${KIT_NOTIFY_WAIT_MAX:-180}"
POLL="${KIT_NOTIFY_POLL:-10}"

usage() {
  sed -n '/^# Usage/,/^# Exit 2/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit 2
}
die() {
  echo "kit-notify: $*" >&2
  exit 1
}

gateway() {
  GATEWAY="$(openclaw_gateway_id)" || die "cannot pick the openclaw-gateway container"
  [ -n "$GATEWAY" ] || die "no running openclaw-gateway container in compose project openclaw"
}

# pick_json KEY: the first JSON object on stdin carrying KEY (the CLI may
# interleave log lines with its JSON).
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

# job_field ID FIELD: print a top-level field of the listed job, or nothing.
job_field() {
  docker exec "$GATEWAY" openclaw cron list --all --json 2>/dev/null | pick_json jobs |
    python3 -c '
import json, sys
job_id, field = sys.argv[1:3]
for job in json.load(sys.stdin)["jobs"]:
    if job.get("id") == job_id:
        value = job.get(field)
        if value is None:
            value = (job.get("state") or {}).get(field)
        print("" if value is None else value)
        break' "$1" "$2"
}

cmd_send() {
  local wait=0 text="" name argv out job_id next fire_s
  while [ $# -gt 0 ]; do
    case "$1" in
      --at)
        [ $# -ge 2 ] || usage
        AT="$2"
        shift 2
        ;;
      --wait)
        wait=1
        shift
        ;;
      --)
        shift
        text="$*"
        break
        ;;
      -*) usage ;;
      *)
        text="$*"
        break
        ;;
    esac
  done
  [ -n "${text//[[:space:]]/}" ] || usage

  gateway
  name="kit-notify-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  argv="$(python3 -c 'import json, sys; print(json.dumps(["echo", sys.argv[1]]))' "$text")"
  out="$(docker exec "$GATEWAY" openclaw cron add \
    --name "$name" \
    --display-name "Kit one-shot notice" \
    --description "A one-shot notice from Kit to the owner, kept after it runs so its delivery can be checked. Scheduled by scripts/kit-notify.sh (source: lifekit-stack)." \
    --agent "$AGENT" --session isolated \
    --at "$AT" --keep-after-run \
    --command-argv "$argv" \
    --timeout-seconds 60 --no-output-timeout-seconds 30 --output-max-bytes 4000 \
    --announce --channel telegram --delivery-target owner \
    --json 2>&1)" || die "cron add failed: $(tail -n 3 <<<"$out")"

  job_id="$(pick_json id <<<"$out" | python3 -c 'import json, sys; print(json.load(sys.stdin)["id"])' 2>/dev/null)" ||
    job_id="$(docker exec "$GATEWAY" openclaw cron list --all --json 2>/dev/null | pick_json jobs |
      python3 -c '
import json, sys
print(next((j["id"] for j in json.load(sys.stdin)["jobs"] if j.get("name") == sys.argv[1]), ""))' "$name")"
  [ -n "$job_id" ] || die "cron add ran but no '$name' job is listed"

  next="$(job_field "$job_id" nextRunAtMs)"
  fire_s=$(($(date +%s) + 60))
  if [[ "$next" =~ ^[0-9]+$ ]]; then fire_s=$((next / 1000)); fi
  echo "scheduled: job $job_id, agent $AGENT, fires at $(date -u -d "@$fire_s" +%Y-%m-%dT%H:%M:%SZ), announce -> telegram owner"
  echo "check:     $0 check $job_id"

  [ "$wait" = 1 ] || return 0
  local deadline=$((fire_s + WAIT_MAX)) rc now
  now="$(date +%s)"
  [ "$now" -ge "$fire_s" ] || sleep $((fire_s - now))
  while :; do
    rc=0
    check_once "$job_id" || rc=$?
    [ "$rc" = 3 ] || return "$rc"
    [ "$(date +%s)" -lt "$deadline" ] || {
      echo "not-yet-run: still no finished run ${WAIT_MAX}s after the fire time" >&2
      return 3
    }
    sleep "$POLL"
  done
}

# check_once ID: report delivered (0) / failed (1) / not-yet-run (3).
check_once() {
  local job_id="$1" out verdict rc=0
  out="$(docker exec "$GATEWAY" openclaw cron runs "$job_id" --json 2>&1)" ||
    die "cron runs $job_id failed: $(tail -n 3 <<<"$out")"
  verdict="$(pick_json entries <<<"$out" | python3 -c '
import json, re, sys
runs = [e for e in json.load(sys.stdin)["entries"] if e.get("action", "finished") == "finished"]
if not runs:
    print("not-yet-run: no finished run recorded yet")
    sys.exit(3)
run = max(runs, key=lambda e: e.get("ts") or 0)
when = run.get("tsIso") or run.get("ts")
if run.get("deliveryStatus") == "delivered" and run.get("delivered") is not False:
    d = (run.get("delivery") or {}).get("resolved") or {}
    where = "/".join(str(d[k]) for k in ("channel", "accountId") if d.get(k))
    print(f"delivered: run finished {when}" + (f" via {where}" if where else ""))
    sys.exit(0)
reason = str(run.get("deliveryError") or run.get("error")
             or run.get("deliverySuppressionReason") or run.get("errorReason")
             or "no reason recorded")
# The gateway names the resolved chat in its errors ("chat_id=<id>",
# "telegram:<id>"): mask every chat-id-shaped number so none is printed.
reason = re.sub(r"-?\b\d{5,}\b", "<chat-id>", reason)
status, delivery = run.get("status"), run.get("deliveryStatus")
print(f"failed: run finished {when}, status={status}, delivery={delivery}: {reason}")
sys.exit(1)')" || rc=$?
  [ -n "$verdict" ] || die "could not read the run history of $job_id"
  case "$rc" in
    0)
      echo "$verdict"
      docker exec "$GATEWAY" openclaw cron rm "$job_id" --json >/dev/null ||
        die "delivered, but deleting job $job_id failed; remove it by hand"
      echo "deleted: job $job_id"
      ;;
    3) echo "$verdict" ;;
    *) echo "$verdict" >&2 ;;
  esac
  return "$rc"
}

cmd_check() {
  if [ $# -ne 1 ] || [ -z "$1" ]; then usage; fi
  gateway
  check_once "$1"
}

[ $# -ge 1 ] || usage
sub="$1"
shift
case "$sub" in
  send) cmd_send "$@" ;;
  check) cmd_check "$@" ;;
  *) usage ;;
esac
