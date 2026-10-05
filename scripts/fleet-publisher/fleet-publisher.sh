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
# and data/. CAPTAIN_HOLD (default FM_HOME/bin/fm-captain-hold.sh) and
# TASKS_AXI (default: tasks-axi on PATH, resolved to an absolute path) are
# read-only CLIs; tasks-axi runs from FM_HOME, where it finds the backlog.
# REPORT_ROOT (default FM_HOME) is what landed report_path values are
# relative to; REPORT_URL_BASE (default "reports") prefixes published report
# URLs. LAVISH_STATE defaults to ~/.lavish-axi/state.json. FLEET_NOW pins the
# clock for tests. The fleet home is only ever read.
#
# Writes OUT_DIR/home-summary.json: the source summary, unchanged, plus
# board_url on every open decision (an open Lavish session fed by the
# captain-hold binding, else the one whose file lives under
# DATA_DIR/<decision id>/, else null), report_url on every landed item (the
# finished report.md copied to OUT_DIR/reports/<id>/report.md, else null) and
# published_epoch.
# The dashboard never reads Lavish or fleet-home state. Writes TEXTFILE_DIR/fleet.prom:
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
CAPTAIN_HOLD="${CAPTAIN_HOLD:-${FM_HOME:+$FM_HOME/bin/fm-captain-hold.sh}}"
TASKS_AXI="${TASKS_AXI:-$(command -v tasks-axi || true)}"
REPORT_ROOT="${REPORT_ROOT:-${FM_HOME:-$(dirname "$DATA_DIR")}}"
REPORT_URL_BASE="${REPORT_URL_BASE:-reports}"
LAVISH_STATE="${LAVISH_STATE:-$HOME/.lavish-axi/state.json}"
NOW="${FLEET_NOW:-$(date +%s)}"
CREDENTIAL_RE='sk-ant-|gh[pousr]_[A-Za-z0-9]{20}|github_pat_|xox[abprs]-|tskey-|AKIA[0-9A-Z]{16}|BEGIN [A-Z ]*PRIVATE KEY|bot[0-9]{6,}:[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]{10,}\.eyJ'
SLUG_RE='^[A-Za-z0-9][A-Za-z0-9._-]*$'

jq -e '(.schema // "" | startswith("fm-secondmate-home-summary.")) and (.generated_epoch | type == "number")' \
  "$SUMMARY_FILE" > /dev/null || {
  echo "fleet-publisher: $SUMMARY_FILE is not a home summary" >&2
  exit 1
}
# The summary carries task names and reasons only. Refuse to publish
# world-readable if a credential ever lands in it.
if grep -Eq "$CREDENTIAL_RE" "$SUMMARY_FILE"; then
  echo "fleet-publisher: $SUMMARY_FILE looks like it holds a credential; not publishing" >&2
  exit 3
fi

summary_tmp="$(mktemp "$OUT_DIR/.home-summary.XXXXXX")"
prom_tmp="$(mktemp "$TEXTFILE_DIR/.fleet.XXXXXX")"
work="$(mktemp -d)"
reports_tmp="$(mktemp -d "$OUT_DIR/.reports.XXXXXX")"
trap 'rm -rf "$summary_tmp" "$prom_tmp" "$work" "$reports_tmp"' EXIT
# The ledger is megabytes: hand it to jq as files, never as arguments.
{ [[ -r "$LAVISH_STATE" ]] && jq -c 'objects' "$LAVISH_STATE" 2> /dev/null; } > "$work/lavish.json" || true
: > "$work/ledger.jsonl"
[[ -r "$LEDGER_FILE" ]] && jq -cR 'fromjson? | objects' "$LEDGER_FILE" > "$work/ledger.jsonl"

# Everything below is read-only access to the fleet home. A source that is
# missing or fails only drops the field it feeds; it never fails the publish.
tsv_to_object() { jq -Rn '[inputs | split("\t") | select(length == 2) | {(.[0]): .[1]}] | add // {}'; }

# Open Lavish sessions the captain-hold binding feeds: session -> origin.
# Source id convention: lavish-<last segment of the session url>.
: > "$work/bound.tsv"
if [[ -n "$CAPTAIN_HOLD" && -x "$CAPTAIN_HOLD" ]]; then
  while IFS= read -r seg; do
    [[ "$seg" =~ $SLUG_RE ]] || continue
    origin="$(timeout 10 "$CAPTAIN_HOLD" binding "lavish-$seg" 2> /dev/null)" || continue
    printf '%s\t%s\n' "$seg" "${origin:-(any)}" >> "$work/bound.tsv"
  done < <(jq -r '(.sessions // {}) | to_entries[] | .value
    | select(.status != "ended" and (.url | type == "string")) | .url | split("/") | last' "$work/lavish.json")
fi
tsv_to_object < "$work/bound.tsv" > "$work/bound.json"

# A bound board need not live under the decision's own directory: the hold
# records the origin task whose directory holds it. Look that up only for
# decisions the directory lookup cannot place, and only when a board is bound.
# tasks-axi finds the backlog from its working directory, so it runs from
# FM_HOME; a failed lookup is logged and the decision falls back to the
# directory lookup.
: > "$work/origins.tsv"
if [[ -s "$work/bound.tsv" ]]; then
  if [[ -z "$FM_HOME" || ! -x "$TASKS_AXI" ]]; then
    echo "fleet-publisher: no FM_HOME or executable TASKS_AXI; hold origins are not looked up" >&2
  else
    while IFS= read -r id; do
      if out="$(cd "$FM_HOME" && PATH="$(dirname "$TASKS_AXI"):$PATH" timeout 10 "$TASKS_AXI" show "$id" --full 2> "$work/tasks-axi.err")"; then
        origin="$(sed -n 's/.*Captain hold origin: \([A-Za-z0-9._-]*\).*/\1/p' <<< "$out" | head -n 1)"
        [[ -z "$origin" ]] || printf '%s\t%s\n' "$id" "$origin" >> "$work/origins.tsv"
      else
        echo "fleet-publisher: tasks-axi show $id failed: $(head -c 200 "$work/tasks-axi.err")" >&2
      fi
    done < <(jq -r --slurpfile lavs "$work/lavish.json" --arg data "$DATA_DIR" '
      ($lavs[0].sessions // {} | [.[] | select(.status != "ended" and (.file | type == "string")) | .file]) as $files
      | (.decisions_open // [])[] | .id as $id | select(($files | map(select(startswith($data + "/" + $id + "/"))) | length) == 0) | $id' \
      "$SUMMARY_FILE" | grep -E "$SLUG_RE" || true)
  fi
fi
tsv_to_object < "$work/origins.tsv" > "$work/origins.json"

# Finished reports: copy each landed item's report into OUT_DIR/reports/<id>/
# so a browser can open it. Only a regular .md file inside the fleet home's
# data directory, free of credential patterns, is served.
: > "$work/reports.tsv"
data_real="$(realpath -e "$DATA_DIR" 2> /dev/null || true)"
while IFS=$'\t' read -r id path; do
  [[ "$id" =~ $SLUG_RE && "$id" != *..* && -n "$data_real" && "$path" == *.md && "$path" != /* && "/$path/" != *"/../"* ]] || continue
  src="$(realpath -e "$REPORT_ROOT/$path" 2> /dev/null)" || continue
  [[ "$src" == "$data_real"/* && -f "$src" && -s "$src" && $(stat -c %s "$src") -le 2097152 ]] || continue
  mkdir -p "$reports_tmp/$id"
  cp "$src" "$reports_tmp/$id/report.md"
  if grep -Eq "$CREDENTIAL_RE" "$reports_tmp/$id/report.md"; then
    rm -rf "${reports_tmp:?}/$id"
    continue
  fi
  printf '%s\t%s\n' "$id" "${REPORT_URL_BASE%/}/$id/report.md" >> "$work/reports.tsv"
done < <(jq -r '(.landed // [])[] | select((.report_path | type) == "string") | [.id, .report_path] | @tsv' "$SUMMARY_FILE")
tsv_to_object < "$work/reports.tsv" > "$work/reports.json"

jq --slurpfile lavs "$work/lavish.json" --arg data "$DATA_DIR" --argjson now "$NOW" \
  --slurpfile bound "$work/bound.json" --slurpfile origins "$work/origins.json" \
  --slurpfile reports "$work/reports.json" '
  ($lavs[0] // {}) as $lav
  | $bound[0] as $bound | $origins[0] as $origins | $reports[0] as $reports
  | def open_sessions:
    [($lav.sessions // {}) | to_entries[] | .value
     | select(.status != "ended" and (.file | type == "string") and (.url | type == "string"))];
  def newest: sort_by(.updated_at // "") | last | .url // null;
  def under($s; $dir): $s.file | startswith($data + "/" + $dir + "/");
  def seg: .url | split("/") | last;
  # A bound session feeds the hold it is bound to, or the hold whose origin
  # task it is bound to or lives under; the directory lookup is the fallback.
  def board($id):
    ($origins[$id] // null) as $o
    | ([open_sessions[] | select($bound[seg] != null)
        | select(($bound[seg] == $id) or ($o != null and ($bound[seg] == $o or under(.; $o))))] | newest)
      // ([open_sessions[] | select(under(.; $id))] | newest);
  .decisions_open |= map(. + {board_url: board(.id)})
  | if (.landed | type) == "array" then .landed |= map(. + {report_url: ($reports[.id] // null)}) else . end
  | .published_epoch = $now' "$SUMMARY_FILE" > "$summary_tmp"
grep -Eq "$CREDENTIAL_RE" "$summary_tmp" && {
  echo "fleet-publisher: the published summary looks like it holds a credential; not publishing" >&2
  exit 3
}

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
# Served reports first, so the summary never names a report that is not there.
# Files are replaced one rename at a time and only unlisted ids are removed, so
# a report the previous summary linked is never briefly missing.
if [[ -s "$work/reports.tsv" ]]; then
  install -d -m 755 "$OUT_DIR/reports"
  while IFS=$'\t' read -r id _; do
    install -d -m 755 "$OUT_DIR/reports/$id"
    install -m 644 "$reports_tmp/$id/report.md" "$OUT_DIR/reports/$id/.report.md.new"
    mv -f "$OUT_DIR/reports/$id/.report.md.new" "$OUT_DIR/reports/$id/report.md"
  done < "$work/reports.tsv"
  for stale in "$OUT_DIR"/reports/*/; do
    [[ -d "$stale" ]] || continue
    stale_id="$(basename "$stale")"
    cut -f1 "$work/reports.tsv" | grep -qxF -- "$stale_id" || rm -rf "$stale"
  done
else
  rm -rf "$OUT_DIR/reports"
fi
mv -f "$summary_tmp" "$OUT_DIR/home-summary.json"
mv -f "$prom_tmp" "$TEXTFILE_DIR/fleet.prom"
