#!/bin/sh
# nudge.sh - the gym-day evening check behind kit's workout-nudge cron
# (docs/runbook.md, "Kit's workout log and gym-day nudge").
#
# The cron fires only on gym days (its day-of-week field is the gym-days
# setting, host state in the gateway's cron store), so this script only asks
# workout-claw whether today has a session:
#   - none logged -> one nudge line (the cron announces it to Telegram)
#   - logged      -> NO_REPLY, which the scheduler suppresses
#   - workout-claw fails or prints something unrecognised -> its error on
#     stderr and exit 1, so the run records an error and the job's failure
#     alert fires instead of a wrong nudge.
#
# "Today" is workout-claw's own default: the local date in the gateway's TZ.
set -u

out="$(workout-claw summary 2>&1)" || {
  echo "Workout check failed: $(printf '%s\n' "$out" | head -n 1)" >&2
  exit 1
}

# summary prints `sessions: 0` when nothing is logged and `sessions:` followed
# by a list when something is.
if printf '%s\n' "$out" | grep -Eq '^sessions: *(0|\[\]) *$'; then
  echo "Gym day and no workout logged yet. Send what you did in one line or a voice note, e.g. \"bench press 8,8,6@60kg, squat 5x5@100kg\"."
elif printf '%s\n' "$out" | grep -Eq '^sessions: *$'; then
  echo NO_REPLY
else
  echo "Workout check failed: unrecognised workout-claw summary output" >&2
  exit 1
fi
