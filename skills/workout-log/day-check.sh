#!/bin/sh
# day-check.sh - the evening day check, printed whole so the evening-day-plan
# cron needs no model turn (docs/runbook.md, "Kit's evening day plan").
#
#   day-check.sh <kcal-target> <protein-target-g>
#
# Runs `nutrition-claw summary` and `workout-claw summary` for today (the
# gateway's local date, `date +%F`; both CLIs require --date) and prints the one delivery line:
#   Day check | nutrition 1840kcal/2278 160g -> 112g under | gym Wed: done (legs) | tomorrow's priorities?
# kcal is eaten/target; the protein part is target -> eaten, then under, over
# or on target. The CLI's own goal values win; the arguments are the display targets
# when it prints none (the owner's numbers, never in git).
#
# Either CLI failing, or a summary without the numbers this needs, prints the
# reason on stderr and exits 1, so the run records an error and the job's
# failure alert fires instead of a wrong line.
set -u

case "${1-}:${2-}" in
  *[!0-9:]* | :* | *:) echo "usage: day-check.sh <kcal-target> <protein-target-g>" >&2; exit 64 ;;
esac

fail() { echo "Day check failed: $1" >&2; exit 1; }

today="$(date +%F)"
nut="$(nutrition-claw summary --date "$today" 2>&1)" || fail "nutrition-claw: $(printf '%s\n' "$nut" | head -n 1)"
gym="$(workout-claw summary --date "$today" 2>&1)" || fail "workout-claw: $(printf '%s\n' "$gym" | head -n 1)"

# A number under `summary: <block>: <field>:` in nutrition-claw's YAML, e.g.
# calories_kcal -> consumed (what was eaten) or goal (the CLI's own target).
field() { # $1 = block, $2 = field
  printf '%s\n' "$nut" | awk -v blk="$1" -v fld="$2" '
    $0 ~ "^  " blk ":[[:space:]]*$" { inb = 1; next }
    inb && /^  [^ ]/ { inb = 0 }
    inb && $0 ~ "^    " fld ":[[:space:]]*[0-9]" {
      v = $0; sub(/^[^:]*:[[:space:]]*/, "", v); printf "%d\n", v + 0.5; exit }'
}
kcal="$(field calories_kcal consumed)"
protein="$(field protein_g consumed)"
[ -n "$kcal" ] && [ -n "$protein" ] || fail "unrecognised nutrition-claw summary output"
# The CLI's own goal wins; the arguments are the display targets when it has none.
kcal_goal="$(field calories_kcal goal || true)"; kcal_goal="${kcal_goal:-$1}"
protein_goal="$(field protein_g goal || true)"; protein_goal="${protein_goal:-$2}"

if printf '%s\n' "$gym" | grep -Eq '^(message: no workouts logged|sessions: *(0|\[\]) *)$'; then
  gym_part="no session"
elif printf '%s\n' "$gym" | grep -Eq '^sessions: *$'; then
  groups="$(printf '%s\n' "$gym" | sed -nE 's/^[[:space:]]*(-[[:space:]]*)?muscle:[[:space:]]*//p' | awk '!seen[$0]++' | paste -sd, - | sed 's/,/, /g')"
  [ -n "$groups" ] || groups="$(printf '%s\n' "$gym" | sed -nE 's/^[[:space:]]*(-[[:space:]]*)?muscle_group:[[:space:]]*//p' | awk '!seen[$0]++' | paste -sd, - | sed 's/,/, /g')"
  gym_part="done${groups:+ ($groups)}"
else
  fail "unrecognised workout-claw summary output"
fi

if [ "$protein" -lt "$protein_goal" ]; then verdict=under
elif [ "$protein" -gt "$protein_goal" ]; then verdict=over
else verdict="on target"; fi

echo "Day check | nutrition ${kcal}kcal/${kcal_goal} ${protein_goal}g -> ${protein}g ${verdict} | gym $(date +%a): ${gym_part} | tomorrow's priorities?"
