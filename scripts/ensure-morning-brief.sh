#!/usr/bin/env bash
# ensure-morning-brief.sh — install the morning-brief skill for the devclaw
# agent and idempotently create its cron job.
#
# Why a script and not config: OpenClaw cron jobs live in the gateway's SQLite
# state, not in openclaw.json (verified against openclaw 2026.6.11 — the docs'
# `openclaw automations` CLI does not exist in this version; `openclaw cron add`
# is the installed surface). This script IS the git-side declaration of the job:
# re-running converges (skill copy is overwrite, cron create is skipped when the
# job already exists).
#
# Run on the VPS host as lifekit, from the repo checkout:
#   /srv/lifekit-stack/scripts/ensure-morning-brief.sh
#
# After a fresh skill install/update, restart the gateway so it re-reads the
# skill manifest (plain `restart` is NOT sufficient):
#   cd /srv/lifekit-stack/compose && docker compose \
#     --env-file /srv/openclaw/config/.env -f docker-compose.yml \
#     up -d --force-recreate openclaw-gateway
#
# Env overrides:
#   MORNING_BRIEF_CHAT_ID  Telegram chat id for delivery (default: 422369750)
#   MORNING_BRIEF_CRON     cron expression (default: "0 8 * * *")
#   MORNING_BRIEF_TZ       IANA tz (default: Europe/Dublin — house tz of the
#                          existing jobs; same wall clock as Europe/London)
set -euo pipefail

GATEWAY=compose-openclaw-gateway-1
JOB_NAME=morning-brief
AGENT=devclaw
CHAT_ID="${MORNING_BRIEF_CHAT_ID:-422369750}"
CRON_EXPR="${MORNING_BRIEF_CRON:-0 8 * * *}"
CRON_TZ="${MORNING_BRIEF_TZ:-Europe/Dublin}"

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
skill_src="${repo_root}/skills/morning-brief"
# Host path of the devclaw agent's workspace (mounted into the gateway at
# /home/node/.openclaw/agents/devclaw/workspace).
skill_dest="/srv/openclaw/config/agents/${AGENT}/workspace/skills/morning-brief"

# 1. Install/refresh the skill (idempotent overwrite).
mkdir -p "$skill_dest"
cp -r "$skill_src/." "$skill_dest/"
echo "skill installed → $skill_dest"

# 2. Create the cron job iff absent (matched by name; jq-free on purpose —
#    the gateway image has no jq).
if docker exec "$GATEWAY" openclaw cron list --json 2>/dev/null \
   | grep -q "\"name\"[[:space:]]*:[[:space:]]*\"$JOB_NAME\""; then
  echo "cron '$JOB_NAME' already exists — leaving it untouched"
  echo "(to change schedule/delivery: openclaw cron edit, or delete + re-run)"
  exit 0
fi

# `cron run`/`cron runs` accept the job ID only (not the name) — capture it
# from the add output (openclaw 2026.6.11 prints the created job as JSON).
add_out="$(docker exec "$GATEWAY" openclaw cron add \
  --name "$JOB_NAME" \
  --description "Daily cross-project brief (repos + devclaw live state) with numbered select-to-dispatch recommendations" \
  --cron "$CRON_EXPR" --tz "$CRON_TZ" \
  --agent "$AGENT" --session isolated \
  --message "Morning brief: run the morning-brief skill in BRIEF mode end-to-end — sweep the repos with gh, read devclaw live state, persist briefs/latest.md plus the dated copy, and output the numbered brief as your final message." \
  --announce --channel telegram --to "$CHAT_ID" \
  --best-effort-deliver \
  --failure-alert --failure-alert-channel telegram --failure-alert-to "$CHAT_ID" \
  --timeout-seconds 900)"
echo "$add_out"
job_id="$(printf '%s' "$add_out" | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"

echo "created cron '$JOB_NAME' ($CRON_EXPR @ $CRON_TZ → telegram:$CHAT_ID, agent:$AGENT)"
if [ -n "$job_id" ]; then
  echo "verify: docker exec $GATEWAY openclaw cron run $job_id && docker exec $GATEWAY openclaw cron runs --id $job_id"
else
  echo "verify: docker exec $GATEWAY openclaw cron list   # find the '$JOB_NAME' job ID, then: openclaw cron run <id>"
fi
