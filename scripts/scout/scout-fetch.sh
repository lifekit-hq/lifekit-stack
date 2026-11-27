#!/usr/bin/env bash
# scout-fetch.sh - daily fetch step of the generic scouting pipeline.
#
# Runs feed2exec over the feed list of every vault lens page with `scout: on`,
# keeps only items it has not seen before, and when there are any writes them to
# a candidates file and sends ONE note to firstmate's inbox (count + path). No
# new items: no file, no note, no model call. See scripts/scout/README.md.
#
# A feed seen for the first time (not yet in the lens database: a new lens or
# a source just added to one) is caught up before the normal fetch, so its back
# catalogue is recorded as seen instead of reaching the judge as a backlog.
# Each lens's items are written grouped by feed, newest first, at most LENS_CAP
# per lens and day, shared fairly between its feeds.
#
#   scout-fetch.sh            normal run (the timer)
#   scout-fetch.sh --catchup  record everything currently in the feeds as seen;
#                             sends nothing (reset a lens by hand)
#
# Environment (all optional but FM_INBOX_BIN):
#   FM_INBOX_BIN    path to fm-inbox.sh (required)
#   FM_INBOX_HOME   FM_HOME handed to fm-inbox.sh
#   SCOUT_LENS_DIR  lens pages            (default /srv/memory/system/scout)
#   SCOUT_STATE_DIR state + candidates    (default ~/.local/state/lifekit-scout)
#   FEED2EXEC       the feed2exec binary  (default feed2exec)
#   SCOUT_DATE      override the UTC date (tests)

set -uo pipefail

CATCHUP=0
case "${1:-}" in
  "") ;;
  --catchup) CATCHUP=1 ;;
  *) echo "usage: scout-fetch.sh [--catchup]" >&2; exit 2 ;;
esac

LENS_DIR="${SCOUT_LENS_DIR:-/srv/memory/system/scout}"
STATE="${SCOUT_STATE_DIR:-$HOME/.local/state/lifekit-scout}"
FEED2EXEC="${FEED2EXEC:-feed2exec}"
TODAY="${SCOUT_DATE:-$(date -u +%F)}"
LENS_CAP=60  # the judge's triage cap; the rest go to candidates/<date>.overflow
HERE="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
CAND="$STATE/candidates"

[[ -n "${FM_INBOX_BIN:-}" ]] || { echo "scout-fetch: FM_INBOX_BIN is not set" >&2; exit 2; }
command -v "$FEED2EXEC" >/dev/null 2>&1 || { echo "scout-fetch: $FEED2EXEC not found (run scripts/scout/install-scout.sh)" >&2; exit 2; }
[[ -d "$LENS_DIR" ]] || { echo "scout-fetch: no lens directory $LENS_DIR; nothing to do" >&2; exit 0; }

mkdir -p "$STATE/db" "$STATE/broken" "$CAND"
exec 9>"$STATE/lock"
flock -n 9 || { echo "scout-fetch: another run holds the lock" >&2; exit 0; }
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

note() {  # <request-id> <text>
  FM_HOME="${FM_INBOX_HOME:-${FM_HOME:-}}" "$FM_INBOX_BIN" note --request-id "$1" -- "$2" >/dev/null
}

# Value of a frontmatter key; fails when the page has no frontmatter, prints
# nothing when the key is absent. Unterminated frontmatter returns 3.
fm_get() {  # <file> <key>
  awk -v key="$2" '
    NR == 1 { if ($0 != "---") exit 1; next }
    $0 == "---" { closed = 1; exit }
    !done && index($0, key ":") == 1 { sub(/^[^:]*:[ \t]*/, ""); print; done = 1 }
    END { if (NR >= 1 && !closed && NR > 1) exit 3 }' "$1"
}

# The first fenced ini block under `## Sources`.
sources_block() {
  awk '/^## Sources[ \t]*$/ { s = 1; next }
       s && /^## / { exit }
       s && !f && /^```ini[ \t]*$/ { f = 1; next }
       f && /^```[ \t]*$/ { exit }
       f { print }' "$1"
}

# Why a Sources block cannot be fed to feed2exec safely; prints nothing if fine.
validate_ini() {  # <ini>
  local ini="$1" ph
  grep -Eq '^[[:space:]]*url[[:space:]]*=' "$ini" || { echo "no feed with a url in the Sources block"; return; }
  if grep -Eq '^[[:space:]]*output[[:space:]]*=' "$ini" \
    && grep -E '^[[:space:]]*output[[:space:]]*=' "$ini" | grep -Evq '=[[:space:]]*feed2exec\.plugins\.echo[[:space:]]*$'; then
    echo "output plugin other than feed2exec.plugins.echo"; return
  fi
  while IFS= read -r ph; do
    case "$ph" in
      '{item.link}' | '{item.title}') ;;
      *) echo "unsupported args placeholder $ph (only {item.link} and {item.title})"; return ;;
    esac
  done < <(grep -E '^[[:space:]]*args[[:space:]]*=' "$ini" | grep -Eo '\{[^}]*\}')
  if grep -E '^[[:space:]]*args[[:space:]]*=' "$ini" | grep -Eq '=[[:space:]]*\{'; then
    echo "args must start with a literal source tag"
  fi
}

report_broken() {  # <lens> <reason> - once per breakage, on the transition
  local lens="$1" reason="$2"
  ((CATCHUP)) && return
  [[ -e "$STATE/broken/$lens" ]] && return
  if note "scout-broken-$lens-$TODAY" "scout: lens '$lens' skipped, $reason"; then
    printf '%s\n' "$reason" >"$STATE/broken/$lens"
  fi
}

for page in "$LENS_DIR"/*.md; do
  [[ -f "$page" ]] || continue
  lens="$(basename "$page" .md)"
  fm_get "$page" scout >"$WORK/scout" 2>/dev/null; rc=$?
  if ((rc == 3)); then report_broken "$lens" "frontmatter is not closed"; continue; fi
  ((rc == 0)) || continue                        # no frontmatter: not a lens page
  [[ "$(tr -d '"\047 ' <"$WORK/scout")" == on ]] || continue # off, or the key is absent

  sources_block "$page" >"$WORK/$lens.ini"
  [[ -s "$WORK/$lens.ini" ]] || { report_broken "$lens" "no ini block under '## Sources'"; continue; }
  reason="$(validate_ini "$WORK/$lens.ini")"
  [[ -z "$reason" ]] || { report_broken "$lens" "$reason"; continue; }

  db="$STATE/db/$lens.sqlite"
  if ((CATCHUP)); then
    "$FEED2EXEC" -v --config "$WORK/$lens.ini" --database "$db" fetch --catchup \
      >/dev/null 2>"$WORK/$lens.err"; rc=$?
  else
    # First sighting: catch up the feeds not seen yet, so a new source's back
    # catalogue is recorded as seen, not offered.
    python3 "$HERE/scout_order.py" config "$WORK/$lens.ini" "$db" seed >"$WORK/$lens.seed.ini" \
      || { report_broken "$lens" "cannot read its feed config or database"; continue; }
    if [[ -s "$WORK/$lens.seed.ini" ]]; then
      if ! "$FEED2EXEC" -v --config "$WORK/$lens.seed.ini" --database "$db" fetch --catchup \
        >/dev/null 2>"$WORK/$lens.err"; then
        report_broken "$lens" "first-sighting catch-up failed ($(tail -n1 "$WORK/$lens.err" | cut -c1-160))"
        continue
      fi
      # a feed reached while empty is seen too: its first item must be offered
      python3 "$HERE/scout_order.py" seeded "$WORK/$lens.seed.ini" "$db" "$WORK/$lens.err" \
        || { report_broken "$lens" "cannot record its caught-up feeds"; continue; }
    fi
    # The normal fetch covers only feeds seen before: one the catch-up could
    # not reach today waits for tomorrow's catch-up.
    python3 "$HERE/scout_order.py" config "$WORK/$lens.ini" "$db" fetch >"$WORK/$lens.fetch.ini" \
      || { report_broken "$lens" "cannot read its feed config or database"; continue; }
    rc=0
    : >"$WORK/$lens.out"
    if [[ -s "$WORK/$lens.fetch.ini" ]]; then
      PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}" "$FEED2EXEC" -v --config "$WORK/$lens.fetch.ini" \
        --database "$db" fetch >"$WORK/$lens.out" 2>"$WORK/$lens.err"; rc=$?
    fi
    # feed2exec has already marked whatever it printed as seen, so the printed
    # lines are the only copy: keep every one, even when the run then failed.
    if grep -q '[^[:space:]]' "$WORK/$lens.out"; then
      have=0  # a later same-day run spends what is left of the lens's cap
      [[ -f "$CAND/$TODAY.txt" ]] && have="$(awk -F'\t' -v l="$lens" '$1 == l { n++ } END { print n + 0 }' "$CAND/$TODAY.txt")"
      budget=$((LENS_CAP > have ? LENS_CAP - have : 0))
      if ! python3 "$HERE/scout_order.py" order "$lens" "$budget" "$CAND/$TODAY.overflow" \
        <"$WORK/$lens.out" >"$WORK/$lens.cand"; then
        echo "scout-fetch: ordering lens $lens failed; keeping its items in feed order" >&2
        grep '[^[:space:]]' "$WORK/$lens.out" | cut -f3- | sed "s/^/$lens\t/" >"$WORK/$lens.cand"
      fi
      if [[ -s "$WORK/$lens.cand" ]]; then
        cat "$WORK/$lens.cand" >>"$CAND/$TODAY.txt"
        rm -f "$CAND/$TODAY.sent"
      fi
    fi
  fi
  if ((rc != 0)); then
    report_broken "$lens" "feed2exec failed ($(tail -n1 "$WORK/$lens.err" | cut -c1-160))"
    continue
  fi
  rm -f "$STATE/broken/$lens"
done

((CATCHUP)) && { echo "scout-fetch: catch-up done, nothing sent"; exit 0; }

# Send one note per candidates file not yet announced: today's, plus any whose
# note failed on an earlier day. The request id carries the file's line count, so
# a retry of the same content is a no-op but a later same-day run is announced.
for f in "$CAND"/*.txt; do
  [[ -f "$f" ]] || continue
  day="$(basename "$f" .txt)"
  [[ -e "$CAND/$day.sent" ]] && continue
  lenses="$(cut -f1 "$f" | sort -u | wc -l)"
  count="$(wc -l <"$f")"
  if note "scout-$day-$count" "scout: $count new items from $lenses lens(es); candidates: $f"; then
    : >"$CAND/$day.sent"
  else
    echo "scout-fetch: note for $day failed; will retry on the next run" >&2
    exit 1
  fi
done
exit 0
