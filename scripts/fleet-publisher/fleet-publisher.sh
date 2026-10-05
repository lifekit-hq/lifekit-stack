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
# and data/. STATE_DIR (default FM_HOME/state) holds the per-task .meta and
# .status files and decisions/<decision id>.json ask records. CAPTAIN_HOLD
# (default FM_HOME/bin/fm-captain-hold.sh) and TASKS_AXI (default tasks-axi)
# are read-only CLIs. REPORT_URL_BASE (default "reports") prefixes published
# report URLs. LAVISH_STATE defaults to ~/.lavish-axi/state.json. FLEET_NOW
# pins the clock for tests. The fleet home is only ever read.
#
# Writes OUT_DIR/home-summary.json: the source summary, unchanged, plus the
# fields documented in scripts/fleet-publisher/README.md (schema
# lifekit-fleet-publisher.v1): board_url, ask, restart, title_plain,
# since_epoch, produces, open_url and project, published_epoch, and the
# finished reports landed items name, copied to OUT_DIR/reports/<id>/report.md.
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
STATE_DIR="${STATE_DIR:-${FM_HOME:+$FM_HOME/state}}"
CAPTAIN_HOLD="${CAPTAIN_HOLD:-${FM_HOME:+$FM_HOME/bin/fm-captain-hold.sh}}"
TASKS_AXI="${TASKS_AXI:-tasks-axi}"
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
trap 'rm -rf "$summary_tmp" "$prom_tmp" "$work" "$reports_tmp" "$OUT_DIR/.reports.old"' EXIT
# The ledger is megabytes: hand it to jq as files, never as arguments.
{ [[ -r "$LAVISH_STATE" ]] && jq -c 'objects' "$LAVISH_STATE" 2> /dev/null; } > "$work/lavish.json" || true
: > "$work/ledger.jsonl"
[[ -r "$LEDGER_FILE" ]] && jq -cR 'fromjson? | objects' "$LEDGER_FILE" > "$work/ledger.jsonl"

# Everything below is read-only access to the fleet home. A source that is
# missing or fails only drops the field it feeds; it never fails the publish.
ids() { jq -r "$1" "$SUMMARY_FILE" | grep -E "$SLUG_RE" || true; }
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
: > "$work/origins.tsv"
if [[ -s "$work/bound.tsv" ]]; then
  while IFS= read -r id; do
    origin="$(timeout 10 "$TASKS_AXI" show "$id" --full 2> /dev/null \
      | sed -n 's/.*Captain hold origin: \([A-Za-z0-9._-]*\).*/\1/p' | head -n 1)" || true
    [[ -z "$origin" ]] || printf '%s\t%s\n' "$id" "$origin" >> "$work/origins.tsv"
  done < <(jq -r --slurpfile lavs "$work/lavish.json" --arg data "$DATA_DIR" '
    ($lavs[0].sessions // {} | [.[] | select(.status != "ended" and (.file | type == "string")) | .file]) as $files
    | (.decisions_open // [])[] | .id as $id | select(($files | map(select(startswith($data + "/" + $id + "/"))) | length) == 0) | $id' \
    "$SUMMARY_FILE" | grep -E "$SLUG_RE" || true)
fi
tsv_to_object < "$work/origins.tsv" > "$work/origins.json"

# Structured asks: STATE_DIR/decisions/<id>.json in the notify-relay producer
# shape (docs/decision-contract.md). No record, no ask.
: > "$work/asks.jsonl"
if [[ -n "$STATE_DIR" ]]; then
  while IFS= read -r id; do
    [[ -f "$STATE_DIR/decisions/$id.json" && ! -L "$STATE_DIR/decisions/$id.json" ]] || continue
    jq -c --arg id "$id" 'select(type == "object") | {($id): .}' "$STATE_DIR/decisions/$id.json" >> "$work/asks.jsonl" 2> /dev/null || true
  done < <(ids '(.decisions_open // [])[].id')
fi
jq -s 'add // {}' "$work/asks.jsonl" > "$work/asks.json"

# Working-since time (the task's .meta mtime) and the PR a working task opened
# (the last PR url in its status file).
: > "$work/since.tsv"
: > "$work/prs.tsv"
if [[ -n "$STATE_DIR" ]]; then
  while IFS= read -r id; do
    mtime="$(stat -c %Y "$STATE_DIR/$id.meta" 2> /dev/null)" && printf '%s\t%s\n' "$id" "$mtime" >> "$work/since.tsv"
    pr="$(grep -ohE 'https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/pull/[0-9]+' "$STATE_DIR/$id.status" 2> /dev/null | tail -n 1)" || true
    [[ -z "$pr" ]] || printf '%s\t%s\n' "$id" "$pr" >> "$work/prs.tsv"
  done < <(ids '(.active_children // [])[].id')
fi
tsv_to_object < "$work/since.tsv" > "$work/since.json"
tsv_to_object < "$work/prs.tsv" > "$work/prs.json"

# Finished reports: copy each landed item's report into OUT_DIR/reports/<id>/
# so a browser can open it. Only a regular .md file inside the fleet home's
# data directory, free of credential patterns, is served.
: > "$work/reports.tsv"
data_real="$(realpath -e "$DATA_DIR" 2> /dev/null || true)"
while IFS=$'\t' read -r id path; do
  [[ "$id" =~ $SLUG_RE && "$id" != *..* && -n "$data_real" && "$path" == *.md && "$path" != /* && "/$path/" != *"/../"* ]] || continue
  src="$(realpath -e "$REPORT_ROOT/$path" 2> /dev/null)" || continue
  [[ "$src" == "$data_real"/* && -f "$src" && -s "$src" && $(stat -c %s "$src") -le 2097152 ]] || continue
  grep -Eq "$CREDENTIAL_RE" "$src" && continue
  mkdir -p "$reports_tmp/$id"
  cp "$src" "$reports_tmp/$id/report.md"
  printf '%s\t%s\n' "$id" "${REPORT_URL_BASE%/}/$id/report.md" >> "$work/reports.tsv"
done < <(jq -r '(.landed // [])[] | select((.report_path | type) == "string") | [.id, .report_path] | @tsv' "$SUMMARY_FILE")
tsv_to_object < "$work/reports.tsv" > "$work/reports.json"

jq --slurpfile lavs "$work/lavish.json" --arg data "$DATA_DIR" --argjson now "$NOW" \
  --slurpfile bound "$work/bound.json" --slurpfile origins "$work/origins.json" \
  --slurpfile asks "$work/asks.json" --slurpfile since "$work/since.json" \
  --slurpfile prs "$work/prs.json" --slurpfile reports "$work/reports.json" '
  ($lavs[0] // {}) as $lav
  | $bound[0] as $bound | $origins[0] as $origins | $asks[0] as $asks
  | $since[0] as $since | $prs[0] as $prs | $reports[0] as $reports
  | ($now | strftime("%Y-%m-%d")) as $today
  | def open_sessions:
    [($lav.sessions // {}) | to_entries[] | .value
     | select(.status != "ended" and (.file | type == "string") and (.url | type == "string"))];
  def newest: sort_by(.updated_at // "") | last | .url // null;
  def under($s; $dir): $s.file | startswith($data + "/" + $dir + "/");
  def seg: .url | split("/") | last;
  def dir_board($id): [open_sessions[] | select(under(.; $id))] | newest;
  # The binding feeds a board to a hold whose origin it names, or any hold
  # when it names none; the board still has to be the hold origin'"'"'s own.
  def board($id):
    ($origins[$id] // null) as $o
    | ([open_sessions[] | select($bound[seg] != null)
        | select(($bound[seg] == $id) or ($o != null and ($bound[seg] == $o or under(.; $o)))
                 or under(.; $id))] | newest)
      // dir_board($id);
  # --- plain titles: no commit prefix, id, kind or status word, no cut word
  def strip_lead:
    sub("^\\s*[A-Za-z]+(\\([^)]*\\))?!?:\\s+"; "")
    | sub("^\\s*[A-Za-z0-9._-]+#[0-9]+[:\\s]+"; "")
    | sub("^\\s*#[0-9]+[:\\s]+"; "")
    | sub("^\\s*(?:WIP|DONE|BLOCKED|PAUSED|PARKED|WORKING)\\b[:\\s-]+"; ""; "i");
  def plain($s):
    ($s // "") as $t
    | ($t | strip_lead | strip_lead | strip_lead
          | sub("\\s*\\((?:repo|kind|priority|hold|since|merged|reported|done)\\b[^)]*\\)\\s*$"; "")) as $x
    | if ($x | test("(…|\\.\\.\\.)$")) then
        ($x | sub("(…|\\.\\.\\.)$"; "") | sub("\\s+\\S*$"; "")
            | sub("[\\s,;:(\\[—-]+$"; "")) as $cut
        | if $cut == "" then $x | sub("(…|\\.\\.\\.)$"; "") else $cut + "…" end
      else $x end
    | if . == "" then $t else . end;
  # --- project: repo#N in the title, else the PR url, else the row'"'"'s repo
  def project($s; $pr; $repo):
    ((($s // "") | capture("^\\s*(?:[A-Za-z]+(?:\\([^)]*\\))?!?:\\s+)?(?<r>[A-Za-z][A-Za-z0-9._-]*)#[0-9]+") | .r)
     // (($pr // "") | capture("^https://github\\.com/[^/]+/(?<r>[^/]+)/") | .r)
     // $repo // null);
  # --- restart condition of a held item
  def restart($reason; $until; $blockers; $record):
    ($reason // "") as $r
    | (if $until != null then $until
       else ($r | capture("(?:restart|revisit|until|planned)\\w*\\s+(?:[A-Za-z]{3}\\s+)?(?<d>[0-9]{4}-[0-9]{2}-[0-9]{2})") | .d) // null end) as $date
    | if $record then {kind: "captain_word", text: $r}
      elif ($blockers | length) > 0 then {kind: "after_work", blocker_ids: $blockers, text: $r}
      elif $until != null and $until > $today then {kind: "date", until: $until, text: $r}
      elif ($r | test("proposals?"; "i")) then {kind: "proposals", text: $r}
      elif ($r | test("captain.?s? (word|answer|call|decision|go-ahead)|(waits?|waiting) (on|for) (the )?captain|his (word|answer|call|decision)"; "i"))
        then {kind: "captain_word", text: $r}
      elif $date != null then {kind: "date", until: $date, text: $r}
      else {kind: "event", text: $r} end;
  # --- the decision-contract v1 ask of a record
  def ask($id; $rec; $board):
    ($rec.options // [] | to_entries
     | map(.key as $i | (.value | if type == "string" then {label: .} else . end)
           | {id: ((.id // ($i + 1)) | tostring), label: (.label // ""), recommended: (.recommended == true)})
     | [.[] | select(.recommended)] + [.[] | select(.recommended | not)]) as $opts
    | {question: ($rec.ask // $rec.question // ""), options: $opts,
       free_text_allowed: (($rec.free_text_allowed // $rec.free_text) == true),
       link: ($board // ("/decisions/" + ($id | @uri)))};
  def rows($k; f): if (.[$k] | type) == "array" then .[$k] |= map(f) else . end;
  def epoch_of_date: try (. + "T00:00:00Z" | fromdateiso8601) catch null;
  . as $src
  | ([(.landed // [])[] | .pr_url | select(type == "string")
      | capture("^https://github\\.com/(?<o>[^/]+)/(?<r>[^/]+)/pull/") | {(.r): .o}] | add // {}) as $orgs
  | def issue_url($c):
      ([($c.name // "") | capture("^\\s*(?:[A-Za-z]+(?:\\([^)]*\\))?!?:\\s+)?(?<r>[A-Za-z][A-Za-z0-9._-]*)#(?<n>[0-9]+)")] | first) as $m
      | if $m != null and $orgs[$m.r] != null then "https://github.com/\($orgs[$m.r])/\($m.r)/issues/\($m.n)" else null end;
  (.decisions_open // []) as $dec
  | ([$dec[] | . + {board_url: board(.id)}
      | . as $d
      | ($asks[.id] // null) as $rec
      | restart(.reason; .hold_until; []; $rec != null) as $rs
      | . + {title_plain: plain(.summary), project: project(.summary; null; null), restart: $rs}
      | if $rec != null and $rs.kind == "captain_word" and (.hold_bucket // "live") == "live"
        then . + {ask: ask($d.id; $rec; $d.board_url)} else . end]) as $rows
  | .decisions_open = [$rows[] | select(.ask != null)]
  | .decisions_parked = [$rows[] | select(.ask == null)]
  | .decisions_total = ($dec | length)
  | .holds_total = (.counts.holds // ((.holds // []) | length))
  | rows("holds"; . + {title_plain: plain(.title), project: project(.title; null; null),
                       restart: restart(.reason; null; (.unresolved_blocker_ids // []); false)})
  | rows("active_children";
      . as $c
      | (if $c.kind == "scout" or $c.kind == "investigate" then "report"
         elif $c.kind == "ship" then "pr" else "landing" end) as $produces
      | $prs[$c.id] as $pr
      | . + {title_plain: plain($c.name), since_epoch: ($since[$c.id] // null | if . == null then null else tonumber end),
             produces: $produces, project: project($c.name; $pr; $c.repo),
             open_url: ($pr // (if $produces == "report" then dir_board($c.id) else null end) // issue_url($c))})
  | rows("landed";
      (if .pr_url != null then "pr" elif .report_path != null then "report" else "landing" end) as $produces
      | . + {title_plain: plain(.title), since_epoch: (.completion.date // null | if . == null then null else epoch_of_date end),
             produces: $produces, project: project(.title; .pr_url; null),
             report_url: ($reports[.id] // null),
             open_url: (.pr_url // $reports[.id] // null)})
  | .publisher_schema = "lifekit-fleet-publisher.v1"
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
find "$reports_tmp" -type d -exec chmod 755 {} + -o -type f -exec chmod 644 {} +
if [[ -s "$work/reports.tsv" ]]; then
  rm -rf "$OUT_DIR/.reports.old"
  [[ ! -e "$OUT_DIR/reports" ]] || mv -f "$OUT_DIR/reports" "$OUT_DIR/.reports.old"
  mv -f "$reports_tmp" "$OUT_DIR/reports"
else
  rm -rf "$OUT_DIR/reports"
fi
mv -f "$summary_tmp" "$OUT_DIR/home-summary.json"
mv -f "$prom_tmp" "$TEXTFILE_DIR/fleet.prom"
