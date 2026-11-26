#!/usr/bin/env bash
# check-cron-declarations.sh - does every cron job the repo declares match what
# runs live? (issue 678)
#
# Cron rows live in the gateway's store, not in git; each ensure-*.sh below is
# the git-side declaration of one row (or, for ensure-kit-cron-models.sh, of a
# policy over Kit's rows). This runs each one with `--check`, which writes
# nothing and compares the live row's agent, schedule, enabled flag, payload,
# trigger and delivery with what a converge sets (scripts/lib/cron-check.sh),
# prints one table line per script, and exits 1 when any of them reports drift
# or could not check. deploy.sh runs it on every
# deploy and fails the deploy on a non-zero exit, so a declared job that is
# missing, paused, on another agent, or carrying another prompt is a red deploy,
# not prose in the runbook.
#
# The fix for a drift line is the script it names, run without --check (as
# lifekit, from /srv/lifekit-stack), or a change to that script when the live
# row is the intended design (docs/runbook.md, "Cron declarations check").
#
# Usage: scripts/check-cron-declarations.sh
# Env: CRON_DECLARATIONS overrides the script list (space-separated, relative to
# scripts/), for tests.
set -uo pipefail

here="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

# Every ensure-*.sh that adds or edits a gateway cron row. A new one goes here
# too (scripts/tests/test_cron_check.py fails when one is missing).
DECLARATIONS=(
  ensure-doc-door.sh
  ensure-finance-pulse.sh
  ensure-goal-checkin-solved.sh
  ensure-kit-cron-models.sh
  ensure-learnclaw-morning.sh
  ensure-learnclaw-weekcount.sh
  ensure-leetcode-daily.sh
  ensure-memory-vault-cleanup.sh
  ensure-needs-you-digest.sh
  ensure-planning-reminder.sh
  ensure-today-brief-fleet.sh
)
if [ -n "${CRON_DECLARATIONS:-}" ]; then
  read -r -a DECLARATIONS <<< "$CRON_DECLARATIONS"
fi

failed=0
printf '%-34s %-6s %s\n' "declaration" "state" "detail"
for script in "${DECLARATIONS[@]}"; do
  out="$(bash "$here/$script" --check 2>&1)"
  rc=$?
  case "$rc" in
    0) state=ok ;;
    1) state=drift; failed=$((failed + 1)) ;;
    *) state=error; failed=$((failed + 1)) ;;
  esac
  # The verdict is the script's last line ("ok: ...", "drift: ...", "refusing: ...").
  detail="$(printf '%s\n' "$out" | grep -v '^[[:space:]]*$' | tail -n 1)"
  printf '%-34s %-6s %s\n' "$script" "$state" "${detail:-(no output, exit $rc)}"
done

if [ "$failed" -gt 0 ]; then
  echo "$failed cron declaration(s) do not match live; run each named script without --check, or change it (docs/runbook.md, \"Cron declarations check\")" >&2
  exit 1
fi
echo "all ${#DECLARATIONS[@]} cron declarations match live"
