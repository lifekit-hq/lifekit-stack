#!/usr/bin/env bash
# Publish the fleet summary for the dashboard and Prometheus. Runs from
# lifekit-fleet-publisher.timer every minute as the account that owns the
# fleet homes, same shape as scripts/quota-gauge/claude-quota-gauge.sh.
#
#   arg 1  OUT_DIR       world-readable dir for the summary (default /var/lib/lifekit-fleet)
#   arg 2  TEXTFILE_DIR  node-exporter textfile directory (default /var/lib/node_exporter/textfile)
#
# Inputs (env): FM_HOMES is the space-separated list of fleet homes (absolute
# paths), default FM_HOME alone. A home's id is its directory's basename and
# must be unique. Each home's summary, ledger and data are its
# state/home-summary.json, state/fleet-ledger.jsonl and data/; its
# captain-hold CLI is its bin/fm-captain-hold.sh, and finished report_path
# values are relative to the home. For the first home only, SUMMARY_FILE,
# LEDGER_FILE, DATA_DIR, CAPTAIN_HOLD and REPORT_ROOT override those (tests,
# and a single-home run without FM_HOME). TASKS_AXI (default: tasks-axi on
# PATH, resolved to an absolute path) is a read-only CLI that runs from each
# home, where it finds that home's backlog. REPORT_URL_BASE (default
# "reports") prefixes published report URLs. LAVISH_STATE defaults to
# ~/.lavish-axi/state.json. FLEET_NOW pins the clock for tests. The fleet
# homes are only ever read.
#
# Writes OUT_DIR/home-summary.json (contract in README.md): every home's
# list fields concatenated, each item tagged with home_id; board_url on every
# open decision (an open Lavish session fed by its home's captain-hold
# binding, else the one whose file lives under that home's
# data/<decision id>/, else null), report_url on every landed item (the
# finished report.md copied to OUT_DIR/reports/<id>/report.md, else null),
# homes[] and published_epoch. With one home every other field is its summary
# unchanged; with more, generated_epoch, valid, reason and counts are
# aggregated and the rest are the first home's.
# The dashboard never reads Lavish or fleet-home state. Writes TEXTFILE_DIR/fleet.prom:
#   fleet_workers{state}                          active children by state, all homes
#   fleet_decisions_open                          open captain decisions, all homes
#   fleet_oldest_decision_age_seconds             0 when none are open
#   fleet_usage_limit_events_1h                   ledger lines naming a usage limit in the last hour
#   fleet_summary_valid                           1 when every home's summary calls itself valid
#   fleet_summary_generated_timestamp_seconds     the oldest home's generated_epoch
#   fleet_home_summary_valid{home}                the same per home; 0 for one not published
#   fleet_home_summary_generated_timestamp_seconds{home}
#   fleet_publisher_last_success_timestamp_seconds
# Both files are replaced atomically. A home whose summary is missing,
# malformed, or looks like it carries a credential is left out and listed in
# homes[] as unpublished; the others still publish, and the run exits nonzero
# so the unit's failure shows. When no home can be published nothing is
# written, so the stale-file rules fire instead of the dashboard showing an
# empty fleet.
set -euo pipefail
OUT_DIR="${1:-/var/lib/lifekit-fleet}"
TEXTFILE_DIR="${2:-/var/lib/node_exporter/textfile}"
TASKS_AXI="${TASKS_AXI:-$(command -v tasks-axi || true)}"
REPORT_URL_BASE="${REPORT_URL_BASE:-reports}"
LAVISH_STATE="${LAVISH_STATE:-$HOME/.lavish-axi/state.json}"
NOW="${FLEET_NOW:-$(date +%s)}"
CREDENTIAL_RE='sk-ant-|gh[pousr]_[A-Za-z0-9]{20}|github_pat_|xox[abprs]-|tskey-|AKIA[0-9A-Z]{16}|BEGIN [A-Z ]*PRIVATE KEY|bot[0-9]{6,}:[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]{10,}\.eyJ'
SLUG_RE='^[A-Za-z0-9][A-Za-z0-9._-]*$'

read -r -a homes <<< "${FM_HOMES:-${FM_HOME:-}}"
if ((${#homes[@]} == 0)); then
  [[ -n "${SUMMARY_FILE:-}" ]] || {
    echo "fleet-publisher: set FM_HOMES or FM_HOME (or SUMMARY_FILE)" >&2
    exit 2
  }
  homes=("")
fi
ids=()
for home in "${homes[@]}"; do
  [[ -z "$home" || "$home" == /* ]] || {
    echo "fleet-publisher: fleet home $home is not an absolute path" >&2
    exit 2
  }
  id="$(basename "${home:-$(dirname "$(dirname "$(realpath -m "$SUMMARY_FILE")")")}")"
  [[ "$id" =~ $SLUG_RE && " ${ids[*]} " != *" $id "* ]] || {
    echo "fleet-publisher: fleet home ${home:-$SUMMARY_FILE} needs a unique id; $id is not one" >&2
    exit 2
  }
  ids+=("$id")
done

summary_tmp="$(mktemp "$OUT_DIR/.home-summary.XXXXXX")"
prom_tmp="$(mktemp "$TEXTFILE_DIR/.fleet.XXXXXX")"
work="$(mktemp -d)"
reports_tmp="$(mktemp -d "$OUT_DIR/.reports.XXXXXX")"
trap 'rm -rf "$summary_tmp" "$prom_tmp" "$work" "$reports_tmp"' EXIT
{ [[ -r "$LAVISH_STATE" ]] && jq -c 'objects' "$LAVISH_STATE" 2> /dev/null; } > "$work/lavish.json" || true
for f in homes.jsonl published.jsonl metrics.jsonl reports.tsv; do : > "$work/$f"; done

# Everything below is read-only access to the fleet homes. A source that is
# missing or fails only drops the field it feeds; it never fails the publish.
tsv_to_object() { jq -Rn '[inputs | split("\t") | select(length == 2) | {(.[0]): .[1]}] | add // {}'; }

# publish_home <home dir> <home id> <summary> <ledger> <data dir> <captain-hold> <report root>
# Writes the home's published summary, metrics and homes[] entry under $work
# and copies its reports into $reports_tmp.
publish_home() {
  local home="$1" id="$2" summary_file="$3" ledger_file="$4" data_dir="$5" hold="$6" report_root="$7"
  local hw="$work/home-$id" seg origin out data_real src path rid
  mkdir "$hw"
  # The ledger is megabytes: hand it to jq as files, never as arguments.
  : > "$hw/ledger.jsonl"
  [[ -r "$ledger_file" ]] && jq -cR 'fromjson? | objects' "$ledger_file" > "$hw/ledger.jsonl"

  # Open Lavish sessions this home's captain-hold binding feeds: session -> origin.
  # Source id convention: lavish-<last segment of the session url>. The CLI
  # takes its home from FM_HOME, so each call names this one.
  : > "$hw/bound.tsv"
  if [[ -n "$hold" && -x "$hold" ]]; then
    while IFS= read -r seg; do
      [[ "$seg" =~ $SLUG_RE ]] || continue
      origin="$(FM_HOME="${home:-${FM_HOME:-}}" timeout 10 "$hold" binding "lavish-$seg" 2> /dev/null)" || continue
      printf '%s\t%s\n' "$seg" "${origin:-(any)}" >> "$hw/bound.tsv"
    done < <(jq -r '(.sessions // {}) | to_entries[] | .value
      | select(.status != "ended" and (.url | type == "string")) | .url | split("/") | last' "$work/lavish.json")
  fi
  tsv_to_object < "$hw/bound.tsv" > "$hw/bound.json"

  # A bound board need not live under the decision's own directory: the hold
  # records the origin task whose directory holds it. Look that up only for
  # decisions the directory lookup cannot place, and only when a board is
  # bound. tasks-axi finds the backlog from its working directory, so it runs
  # from the home; a failed lookup is logged and the decision falls back to
  # the directory lookup.
  : > "$hw/origins.tsv"
  if [[ -s "$hw/bound.tsv" ]]; then
    if [[ -z "$home" || ! -x "$TASKS_AXI" ]]; then
      echo "fleet-publisher: $id: no home or executable TASKS_AXI; hold origins are not looked up" >&2
    else
      while IFS= read -r rid; do
        if out="$(cd "$home" && FM_HOME="$home" PATH="$(dirname "$TASKS_AXI"):$PATH" timeout 10 "$TASKS_AXI" show "$rid" --full 2> "$hw/tasks-axi.err")"; then
          origin="$(sed -n 's/.*Captain hold origin: \([A-Za-z0-9._-]*\).*/\1/p' <<< "$out" | head -n 1)"
          [[ -z "$origin" ]] || printf '%s\t%s\n' "$rid" "$origin" >> "$hw/origins.tsv"
        else
          echo "fleet-publisher: $id: tasks-axi show $rid failed: $(head -c 200 "$hw/tasks-axi.err")" >&2
        fi
      done < <(jq -r --slurpfile lavs "$work/lavish.json" --arg data "$data_dir" '
        ($lavs[0].sessions // {} | [.[] | select(.status != "ended" and (.file | type == "string")) | .file]) as $files
        | (.decisions_open // [])[] | .id as $id | select(($files | map(select(startswith($data + "/" + $id + "/"))) | length) == 0) | $id' \
        "$summary_file" | grep -E "$SLUG_RE" || true)
    fi
  fi
  tsv_to_object < "$hw/origins.tsv" > "$hw/origins.json"

  # Finished reports: copy each landed item's report into OUT_DIR/reports/<id>/
  # so a browser can open it. Only a regular .md file inside the home's data
  # directory, free of credential patterns, is served. Report ids share one
  # namespace: on a clash between homes the first home listed keeps it.
  : > "$hw/reports.tsv"
  data_real="$(realpath -e "$data_dir" 2> /dev/null || true)"
  while IFS=$'\t' read -r rid path; do
    [[ "$rid" =~ $SLUG_RE && "$rid" != *..* && -n "$data_real" && "$path" == *.md && "$path" != /* && "/$path/" != *"/../"* ]] || continue
    src="$(realpath -e "$report_root/$path" 2> /dev/null)" || continue
    [[ "$src" == "$data_real"/* && -f "$src" && -s "$src" && $(stat -c %s "$src") -le 2097152 ]] || continue
    if [[ -e "$reports_tmp/$rid" ]]; then
      cut -f1 "$hw/reports.tsv" | grep -qxF -- "$rid" \
        || echo "fleet-publisher: $id: report $rid clashes with an earlier home's; not served" >&2
      continue
    fi
    mkdir -p "$reports_tmp/$rid"
    cp "$src" "$reports_tmp/$rid/report.md"
    if grep -Eq "$CREDENTIAL_RE" "$reports_tmp/$rid/report.md"; then
      rm -rf "${reports_tmp:?}/$rid"
      continue
    fi
    printf '%s\t%s\n' "$rid" "${REPORT_URL_BASE%/}/$rid/report.md" >> "$hw/reports.tsv"
  done < <(jq -r '(.landed // [])[] | select((.report_path | type) == "string") | [.id, .report_path] | @tsv' "$summary_file")
  cat "$hw/reports.tsv" >> "$work/reports.tsv"
  tsv_to_object < "$hw/reports.tsv" > "$hw/reports.json"

  jq -c --slurpfile lavs "$work/lavish.json" --arg data "$data_dir" --arg hid "$id" \
    --slurpfile bound "$hw/bound.json" --slurpfile origins "$hw/origins.json" \
    --slurpfile reports "$hw/reports.json" '
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
    # Every listed item names the home it came from, so an answer can go back there.
    | with_entries(if (.value | type) == "array"
        then .value |= map(if type == "object" then . + {home_id: $hid} else . end) else . end)' \
    "$summary_file" >> "$work/published.jsonl"

  jq -c --slurpfile ledger "$hw/ledger.jsonl" --argjson now "$NOW" --arg hid "$id" --arg path "$home" '
    def opened($d):
      [$ledger[] | select(.task == $d.id and .key != null and .key == $d.key and .state == "needs-decision") | .ts] | min;
    def age($d):
      (opened($d) | if . != null then $now - . else null end)
      // (if $d.hold_age_days != null then $d.hold_age_days * 86400 else null end);
    (.decisions_open // []) as $dec
    | {id: $hid,
       workers: [(.active_children // [])[] | .state],
       decisions: ($dec | length),
       oldest: ([$dec[] | age(.) | numbers] | max),
       usage: ([$ledger[]
         | select(.event == "task.status" and .ts >= ($now - 3600) and .ts <= $now
                  and (((.text // "") + " " + (.key // "")) | test("usage[ -]limit"; "i")))] | length)},
      {id: $hid, path: (if $path == "" then null else $path end), published: true, schema, generated_epoch,
       valid, reason, state, counts}' "$summary_file" > "$hw/rows.jsonl"
  head -n 1 "$hw/rows.jsonl" >> "$work/metrics.jsonl"
  tail -n 1 "$hw/rows.jsonl" >> "$work/homes.jsonl"
}

rc=0
unpublished() { # <home id> <home dir> <exit code> <reason>
  jq -cn --arg id "$1" --arg path "$2" --arg reason "$4" \
    '{id: $id, path: (if $path == "" then null else $path end), published: false, valid: false, reason: $reason}' \
    >> "$work/homes.jsonl"
  ((rc = $3 > rc ? $3 : rc)) || true
}
for i in "${!homes[@]}"; do
  home="${homes[$i]}" id="${ids[$i]}"
  summary_file="$home/state/home-summary.json" ledger_file="$home/state/fleet-ledger.jsonl"
  data_dir="$home/data" hold="$home/bin/fm-captain-hold.sh" report_root="$home"
  if ((i == 0)); then
    summary_file="${SUMMARY_FILE:-$summary_file}" ledger_file="${LEDGER_FILE:-$ledger_file}"
    data_dir="${DATA_DIR:-$data_dir}" hold="${CAPTAIN_HOLD:-${home:+$hold}}"
    report_root="${REPORT_ROOT:-${home:-$(dirname "$data_dir")}}"
  fi
  if ! jq -e '(.schema // "" | startswith("fm-secondmate-home-summary.")) and (.generated_epoch | type == "number")' \
    "$summary_file" > /dev/null; then
    echo "fleet-publisher: $summary_file is not a home summary" >&2
    unpublished "$id" "$home" 1 "summary is missing or not a home summary"
    continue
  fi
  # The summary carries task names and reasons only. Refuse to publish
  # world-readable if a credential ever lands in it.
  if grep -Eq "$CREDENTIAL_RE" "$summary_file"; then
    echo "fleet-publisher: $summary_file looks like it holds a credential; not publishing" >&2
    unpublished "$id" "$home" 3 "summary looks like it holds a credential"
    continue
  fi
  publish_home "$home" "$id" "$summary_file" "$ledger_file" "$data_dir" "$hold" "$report_root"
done
[[ -s "$work/published.jsonl" ]] || exit "$rc"

# One home: its summary as-is. More: the lists concatenated, and the fields
# that describe the whole fleet aggregated over every home.
jq -s --slurpfile homes "$work/homes.jsonl" --argjson now "$NOW" '
  . as $parts
  | if ($homes | length) == 1 then $parts[0]
    else ([$parts[] | to_entries[] | select(.value | type == "array") | .key] | unique) as $lists
      | $parts[0]
      + (reduce $lists[] as $k ({}; .[$k] = [$parts[] | .[$k] | arrays | .[]]))
      + {generated_epoch: ([$parts[].generated_epoch] | min),
         valid: all($homes[]; .valid == true),
         reason: ([$homes[] | select((.reason // "") != "") | "\(.id): \(.reason)"]
                  | if length == 0 then null else join("; ") end)}
      | if any($parts[]; .counts | type == "object") then
          .counts = reduce ($parts[].counts | objects | to_entries[] | select(.value | type == "number")) as $e
            ({}; .[$e.key] += $e.value)
        else . end
    end
  | .homes = $homes | .published_epoch = $now' "$work/published.jsonl" > "$summary_tmp"
grep -Eq "$CREDENTIAL_RE" "$summary_tmp" && {
  echo "fleet-publisher: the published summary looks like it holds a credential; not publishing" >&2
  exit 3
}

jq -rn --slurpfile m "$work/metrics.jsonl" --slurpfile s "$summary_tmp" --argjson now "$NOW" '
  $s[0] as $s
  | ([$m[].workers[]]) as $kids
  | (["working", "idle", "blocked", "paused", "needs-decision"] + $kids | unique) as $states
  | "# TYPE fleet_workers gauge",
    ($states[] as $st | "fleet_workers{state=\"\($st)\"} \([$kids[] | select(. == $st)] | length)"),
    "fleet_decisions_open \([$m[].decisions] | add)",
    "fleet_oldest_decision_age_seconds \([$m[].oldest | numbers] | max // 0)",
    "fleet_usage_limit_events_1h \([$m[].usage] | add)",
    "fleet_summary_valid \(if $s.valid == true then 1 else 0 end)",
    "fleet_summary_generated_timestamp_seconds \($s.generated_epoch)",
    ($s.homes[] | "fleet_home_summary_valid{home=\"\(.id)\"} \(if .valid == true then 1 else 0 end)"),
    ($s.homes[] | select(.published) | "fleet_home_summary_generated_timestamp_seconds{home=\"\(.id)\"} \(.generated_epoch)"),
    "fleet_publisher_last_success_timestamp_seconds \($now)"' > "$prom_tmp"

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
exit "$rc"
