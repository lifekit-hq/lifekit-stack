#!/usr/bin/env bash
# ensure-memory-vault-cleanup.sh — idempotently create the weekly
# memory-vault-cleanup cron: the judgment pass that runs the vault's own
# memory-audit skill against the report memory_vault_audit just wrote, so
# Rule 3's judgment deletions in audits/latest.md get worked instead of only
# proposed (vault README.md, "Rule 3 - rotation policy").
#
# Why a script and not config: cron jobs live in the gateway's SQLite state,
# not in openclaw.json — same split as ensure-morning-brief.sh and
# ensure-finance-pulse.sh. This script IS the git-side declaration of the
# job: re-running converges (create is skipped when the job already exists).
#
# Why an agentTurn on the existing kit agent, not a new agent/skill/service:
# the vault (/srv/memory) is already bind-mounted read-write into the gateway
# at /home/node/memory for every agent (compose/docker-compose.yml), and kit
# already owns the command-kind memory_vault_audit cron. The vault's own
# .claude/skills/memory-audit/SKILL.md is plain instructions on a mounted
# path — the agent turn reads and follows it directly; nothing gets
# installed into any agent workspace, so no skill-manifest reload or
# container restart is needed.
#
# Why 20 minutes after memory_vault_audit (host state: "30 3 * * 0"
# Europe/Dublin — see scripts/memory-audit/README.md): the audit itself
# finishes in well under a minute (observed ~38s on this host). The offset
# is buffer, not a measured dependency — the cron store has no cross-job
# "wait for" primitive, so the two jobs are ordered by clock offset alone.
# This job's own prompt also checks audits/latest.md's date before doing
# anything, so a slow or failed audit run is a no-op here, not a race.
#
# Run on the VPS host as lifekit, from the repo checkout:
#   /srv/lifekit-stack/scripts/ensure-memory-vault-cleanup.sh
#
# Env overrides:
#   MEMORY_VAULT_CLEANUP_CHAT_ID  Telegram chat id for delivery/failure
#                                 alerts (default: $LIFEKIT_TELEGRAM_CHAT
#                                 from the host env file; required — never
#                                 hardcode a chat id here)
#   MEMORY_VAULT_CLEANUP_CRON     cron expression (default: "50 3 * * 0")
#   MEMORY_VAULT_CLEANUP_TZ       IANA tz (default: Europe/Dublin — matches
#                                 memory_vault_audit)
set -euo pipefail

GATEWAY=compose-openclaw-gateway-1
JOB_NAME=memory-vault-cleanup
AGENT=kit
CHAT_ID="${MEMORY_VAULT_CLEANUP_CHAT_ID:-${LIFEKIT_TELEGRAM_CHAT:-}}"
CRON_EXPR="${MEMORY_VAULT_CLEANUP_CRON:-50 3 * * 0}"
CRON_TZ="${MEMORY_VAULT_CLEANUP_TZ:-Europe/Dublin}"

if [ -z "$CHAT_ID" ]; then
  echo "no chat id: set MEMORY_VAULT_CLEANUP_CHAT_ID or LIFEKIT_TELEGRAM_CHAT (host env file) — never hardcode one in git" >&2
  exit 1
fi

MESSAGE="Weekly memory-vault cleanup: work the judgment items from this week's vault audit using the vault's own memory-audit skill. The vault is mounted at /home/node/memory; read /home/node/memory/.claude/skills/memory-audit/SKILL.md in full first and follow it exactly -- it is the authority on procedure, this message only scopes the run. Precondition: read /home/node/memory/audits/latest.md's frontmatter updatedAt; if it is not today's UTC date, the audit that should have produced it did not run this week -- apply nothing, reply exactly NO_REPLY, and stop. Otherwise skip the skill's own step 0 (the audit already ran minutes ago); go straight to its mechanical-findings, contradiction, rotation-judgment and archive-pass sections, applying only what the skill says to apply directly -- anything it says needs Denys stays unapplied. Do not run the memory-defrag skill or any other vault skill; this run is memory-audit only. Hard rules, no exceptions: never edit sources/, never edit PLAN.md, never edit a generated block between openclaw markers, bump a page's updatedAt only when you changed its content, never rewrite git history. Finish the skill's own way: re-run sh bin/audit.sh to confirm 0 high / 0 medium, then append exactly one log.md line via bin/log.sh in the skill's format saying what you changed, or that nothing needed changing. Leave the edit uncommitted -- the host's memory-sync.timer commits and pushes the vault on its own schedule; never run git commit or git push yourself. If nothing beyond that routine log line needs Denys, your entire final reply must be exactly NO_REPLY. If something does (a real contradiction, a proposal to grade, anything else the skill lists under Needs Denys), send exactly one Telegram message covering it, then stop -- no extra messages or recaps."

# Create the cron job iff absent (matched by name; jq-free on purpose — the
# gateway image has no jq).
if docker exec "$GATEWAY" openclaw cron list --all --json 2>/dev/null \
   | grep -q "\"name\"[[:space:]]*:[[:space:]]*\"$JOB_NAME\""; then
  echo "cron '$JOB_NAME' already exists — leaving it untouched"
  echo "(to change schedule/message: openclaw cron edit, or delete + re-run)"
  exit 0
fi

add_out="$(docker exec "$GATEWAY" openclaw cron add \
  --name "$JOB_NAME" \
  --display-name "Memory vault cleanup" \
  --description "Weekly judgment pass over the vault's memory-audit findings (README Rule 3): runs the vault's memory-audit skill on the report memory_vault_audit just wrote, applies what it applies, logs one line. Declared by scripts/ensure-memory-vault-cleanup.sh (source: lifekit-stack)." \
  --cron "$CRON_EXPR" --tz "$CRON_TZ" \
  --agent "$AGENT" --session isolated \
  --message "$MESSAGE" \
  --timeout-seconds 1200 \
  --announce --channel telegram --to "$CHAT_ID" \
  --best-effort-deliver \
  --json)"
echo "$add_out"
job_id="$(printf '%s' "$add_out" | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"

echo "created cron '$JOB_NAME' ($CRON_EXPR @ $CRON_TZ -> telegram:$CHAT_ID, agent:$AGENT)"

if [ -n "$job_id" ]; then
  docker exec "$GATEWAY" openclaw cron edit "$job_id" \
    --failure-alert --failure-alert-after 2 \
    --failure-alert-channel telegram --failure-alert-to "$CHAT_ID" \
    --failure-alert-mode announce --failure-alert-cooldown 6h \
    --failure-alert-exclude-skipped
  echo "failure alert set (after 2 consecutive errors, 6h cooldown, telegram:$CHAT_ID)"
  echo "verify: docker exec $GATEWAY openclaw cron run $job_id --expect-final --json"
  echo "        docker exec $GATEWAY openclaw cron show $job_id"
else
  echo "could not parse job id from cron add output — set the failure alert by hand:" >&2
  echo "  docker exec $GATEWAY openclaw cron list --all --json   # find the '$JOB_NAME' job id" >&2
  echo "  docker exec $GATEWAY openclaw cron edit <id> --failure-alert --failure-alert-after 2 --failure-alert-channel telegram --failure-alert-to $CHAT_ID --failure-alert-mode announce --failure-alert-cooldown 6h --failure-alert-exclude-skipped" >&2
fi
