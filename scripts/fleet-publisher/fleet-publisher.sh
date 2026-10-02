#!/usr/bin/env bash
# Publish the fleet summary for the dashboard and Prometheus. Runs from
# lifekit-fleet-publisher.timer every minute as the account that owns the
# fleet home, same shape as scripts/quota-gauge/claude-quota-gauge.sh.
#
#   arg 1  OUT_DIR       world-readable dir for the summary (default /var/lib/lifekit-fleet)
#   arg 2  TEXTFILE_DIR  node-exporter textfile directory (default /var/lib/node_exporter/textfile)
#
# Inputs (env): FM_HOME is the fleet home; SUMMARY_FILE, LEDGER_FILE and
# DATA_DIR default to its state/home-summary.json, state/fleet-ledger.jsonl
# and data/. LAVISH_STATE defaults to ~/.lavish-axi/state.json. FLEET_NOW
# pins the clock for tests.
#
# Writes OUT_DIR/home-summary.json: the source summary with a board_url on
# every open decision (the open Lavish session whose file lives under
# DATA_DIR/<decision id>/, else null) and published_epoch, so the dashboard
# never reads Lavish state. Writes TEXTFILE_DIR/fleet.prom:
#   fleet_workers{state}                          active children by state
#   fleet_decisions_open                          open captain decisions
#   fleet_oldest_decision_age_seconds             0 when none are open
#   fleet_usage_limit_events_1h                   ledger lines naming a usage limit in the last hour
#   fleet_summary_valid                           1 when the source summary calls itself valid
#   fleet_summary_generated_timestamp_seconds     the source summary's generated_epoch
#   fleet_publisher_last_success_timestamp_seconds
# Both files are replaced atomically. A missing or malformed source, or one
# that looks like it carries a credential, exits nonzero and leaves the
# previous files in place, so the stale-file rules fire instead of the
# dashboard showing an empty fleet.
set -euo pipefail
OUT_DIR="${1:-/var/lib/lifekit-fleet}"
TEXTFILE_DIR="${2:-/var/lib/node_exporter/textfile}"
FM_HOME="${FM_HOME:-}"
[[ -n "$FM_HOME" || -n "${SUMMARY_FILE:-}" ]] || {
  echo "fleet-publisher: set FM_HOME (or SUMMARY_FILE)" >&2
  exit 2
}
SUMMARY_FILE="${SUMMARY_FILE:-$FM_HOME/state/home-summary.json}"
LEDGER_FILE="${LEDGER_FILE:-$FM_HOME/state/fleet-ledger.jsonl}"
DATA_DIR="${DATA_DIR:-$FM_HOME/data}"
LAVISH_STATE="${LAVISH_STATE:-$HOME/.lavish-axi/state.json}"
NOW="${FLEET_NOW:-$(date +%s)}"

jq -e '(.schema // "" | startswith("fm-secondmate-home-summary.")) and (.generated_epoch | type == "number")' \
  "$SUMMARY_FILE" > /dev/null || {
  echo "fleet-publisher: $SUMMARY_FILE is not a home summary" >&2
  exit 1
}
# The summary carries task names and reasons only. Refuse to publish
# world-readable if a credential ever lands in it.
if grep -Eq 'sk-ant-|gh[pousr]_[A-Za-z0-9]{20}|github_pat_|xox[abprs]-|tskey-|AKIA[0-9A-Z]{16}|BEGIN [A-Z ]*PRIVATE KEY|bot[0-9]{6,}:[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]{10,}\.eyJ' "$SUMMARY_FILE"; then
  echo "fleet-publisher: $SUMMARY_FILE looks like it holds a credential; not publishing" >&2
  exit 3
fi

summary_tmp="$(mktemp "$OUT_DIR/.home-summary.XXXXXX")"
prom_tmp="$(mktemp "$TEXTFILE_DIR/.fleet.XXXXXX")"
work="$(mktemp -d)"
trap 'rm -rf "$summary_tmp" "$prom_tmp" "$work"' EXIT
# The ledger is megabytes: hand it to jq as files, never as arguments.
{ [[ -r "$LAVISH_STATE" ]] && jq -c 'objects' "$LAVISH_STATE" 2> /dev/null; } > "$work/lavish.json" || true
: > "$work/ledger.jsonl"
[[ -r "$LEDGER_FILE" ]] && jq -cR 'fromjson? | objects' "$LEDGER_FILE" > "$work/ledger.jsonl"

jq --slurpfile lavs "$work/lavish.json" --arg data "$DATA_DIR" --argjson now "$NOW" '
  ($lavs[0] // {}) as $lav
  | def board($id):
    ($data + "/" + $id + "/") as $p
    | [($lav.sessions // {}) | to_entries[] | .value
       | select(.status != "ended" and (.file | type == "string") and (.file | startswith($p)))]
    | sort_by(.updated_at // "") | last | .url // null;
  .decisions_open |= map(. + {board_url: board(.id)})
  | .published_epoch = $now' "$SUMMARY_FILE" > "$summary_tmp"

jq -r --slurpfile ledger "$work/ledger.jsonl" --argjson now "$NOW" '
  def opened($d):
    [$ledger[] | select(.task == $d.id and .key != null and .key == $d.key and .state == "needs-decision") | .ts] | min;
  def age($d):
    (opened($d) | if . != null then $now - . else null end)
    // (if $d.hold_age_days != null then $d.hold_age_days * 86400 else null end);
  (.active_children // []) as $kids
  | (["working", "idle", "blocked", "paused", "needs-decision"] + [$kids[].state] | unique) as $states
  | (.decisions_open // []) as $dec
  | "# TYPE fleet_workers gauge",
    ($states[] as $s | "fleet_workers{state=\"\($s)\"} \([$kids[] | select(.state == $s)] | length)"),
    "fleet_decisions_open \($dec | length)",
    "fleet_oldest_decision_age_seconds \([$dec[] | age(.) | numbers] | max // 0)",
    "fleet_usage_limit_events_1h \([$ledger[]
      | select(.event == "task.status" and .ts >= ($now - 3600) and .ts <= $now
               and (((.text // "") + " " + (.key // "")) | test("usage[ -]limit"; "i")))] | length)",
    "fleet_summary_valid \(if .valid == true then 1 else 0 end)",
    "fleet_summary_generated_timestamp_seconds \(.generated_epoch)",
    "fleet_publisher_last_success_timestamp_seconds \($now)"' "$SUMMARY_FILE" > "$prom_tmp"

chmod 644 "$summary_tmp" "$prom_tmp"
mv -f "$summary_tmp" "$OUT_DIR/home-summary.json"
mv -f "$prom_tmp" "$TEXTFILE_DIR/fleet.prom"
