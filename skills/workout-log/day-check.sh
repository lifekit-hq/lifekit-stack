#!/bin/sh
# day-check.sh - the evening day check, printed whole so the evening-day-plan
# cron needs no model turn (docs/runbook.md, "Kit's evening day plan").
#
#   day-check.sh <kcal-target> <protein-target-g>
#
# Runs `nutrition-claw summary` and `workout-claw summary` for today (the
# gateway's local date) and prints the one delivery line:
#   Day check | nutrition 1840kcal/2278 160g -> 112g under | gym Wed: done (legs) | tomorrow's priorities?
# kcal is eaten/target; the protein part is target -> eaten, then under, over
# or on target. The targets come from the cron definition (they are the owner's
# numbers, kept in the vault's health page, never in git).
#
# Either CLI failing, or a summary without the numbers this needs, prints the
# reason on stderr and exits 1, so the run records an error and the job's
# failure alert fires instead of a wrong line.
set -u

kcal_goal="${1-}"
protein_goal="${2-}"
case "$kcal_goal:$protein_goal" in
  *[!0-9:]* | :* | *:) echo "usage: day-check.sh <kcal-target> <protein-target-g>" >&2; exit 64 ;;
esac

fail() { echo "Day check failed: $1" >&2; exit 1; }

nut="$(nutrition-claw summary 2>&1)" || fail "nutrition-claw: $(printf '%s\n' "$nut" | head -n 1)"
gym="$(workout-claw summary 2>&1)" || fail "workout-claw: $(printf '%s\n' "$gym" | head -n 1)"

# The first number under a key, skipping an indented `goal` block (the CLI's
# own copy of the targets is not what was eaten).
eaten() { # $1 = key regexp
  printf '%s\n' "$nut" | awk -v key="^[[:space:]]*-?[[:space:]]*($1)[a-z_]*:" '
    function indent(s) { match(s, /^ */); return RLENGTH }
    /^[[:space:]]*goals?:[[:space:]]*$/ { skip = indent($0); next }
    skip != "" && indent($0) > skip { next }
    { skip = "" }
    $0 ~ key { v = $0; sub(/^[^:]*:[[:space:]]*/, "", v); if (v ~ /^[0-9]+(\.[0-9]+)?/) { printf "%d\n", v + 0.5; exit } }'
}
kcal="$(eaten 'calories|kcal')"
protein="$(eaten 'protein')"
[ -n "$kcal" ] && [ -n "$protein" ] || fail "unrecognised nutrition-claw summary output"

if printf '%s\n' "$gym" | grep -Eq '^sessions: *(0|\[\]) *$'; then
  gym_part="no session"
elif printf '%s\n' "$gym" | grep -Eq '^sessions: *$'; then
  groups="$(printf '%s\n' "$gym" | sed -nE 's/^[[:space:]]*(-[[:space:]]*)?muscle_group:[[:space:]]*//p' | awk '!seen[$0]++' | paste -sd, - | sed 's/,/, /g')"
  gym_part="done${groups:+ ($groups)}"
else
  fail "unrecognised workout-claw summary output"
fi

if [ "$protein" -lt "$protein_goal" ]; then verdict=under
elif [ "$protein" -gt "$protein_goal" ]; then verdict=over
else verdict="on target"; fi

echo "Day check | nutrition ${kcal}kcal/${kcal_goal} ${protein_goal}g -> ${protein}g ${verdict} | gym $(date +%a): ${gym_part} | tomorrow's priorities?"
