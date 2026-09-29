#!/usr/bin/env bash
# ensure-memory-vault-cleanup.sh — idempotently create the weekly
# memory-vault-cleanup cron: the "dreaming" judgment pass that runs the
# vault's own memory-audit skill, unattended, against the report
# memory_vault_audit just wrote, so Rule 3's judgment items in
# audits/latest.md get worked instead of only proposed (vault README.md,
# "Rule 3 - rotation policy").
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
# path — the agent turn reads and follows it directly, applying its whole
# "safe to apply alone" tier unattended (frontmatter/link/size fixes, stale
# restatements, log compaction, stale-status refresh-or-conclude, the
# archive pass) exactly as the skill already specifies; nothing gets
# installed into any agent workspace, so no skill-manifest reload or
# container restart is needed.
#
# Why notify-relay instead of OpenClaw's own --announce/--failure-alert:
# every owner-facing notification in this stack speaks one grammar through
# one renderer (docs/message-format.md) except the named Grafana exemption;
# a cron-level --announce would be a second, un-formatted, every-run Telegram
# path outside that grammar. notify-relay is already reachable from the
# gateway container on the compose network (http://notify-relay:8090/notify,
# confirmed live) with no chat id needed — the relay owns that config. So
# this job never touches Telegram directly and needs no chat-id input at
# all; delivery is a step in the agent's own prompt, not a cron flag.
#
# Why "only when the Needs Denys list changes": the skill's own judgment
# already separates what it applies alone from what it escalates (SKILL.md,
# "Needs Denys" list) — sending that list every week even when it hasn't
# changed is noise, not signal. The agent prompt below keeps the previous
# list in a small vault-side state file (audits/needs-denys-state.json,
# alongside the audit's own generated audits/latest.md + findings.json) and
# only calls notify-relay on a transition — the same poll-and-diff shape
# scripts/alert-inbox uses for Grafana alerts, applied here without a second
# poller: one weekly agent turn already runs the whole compare-and-decide
# step itself, so no new cron or daemon is needed for it.
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
#   MEMORY_VAULT_CLEANUP_CRON  cron expression (default: "50 3 * * 0")
#   MEMORY_VAULT_CLEANUP_TZ    IANA tz (default: Europe/Dublin — matches
#                               memory_vault_audit)
set -euo pipefail

GATEWAY=compose-openclaw-gateway-1
JOB_NAME=memory-vault-cleanup
AGENT=kit
CRON_EXPR="${MEMORY_VAULT_CLEANUP_CRON:-50 3 * * 0}"
CRON_TZ="${MEMORY_VAULT_CLEANUP_TZ:-Europe/Dublin}"

MESSAGE="Weekly memory-vault dreaming pass: run the vault's own memory-audit skill unattended and apply its judgment tiers exactly as it defines them. The vault is mounted at /home/node/memory; read /home/node/memory/.claude/skills/memory-audit/SKILL.md in full first and follow it exactly -- it is the authority on procedure, this message only scopes the run and adds the delivery step it does not cover. Precondition: read /home/node/memory/audits/latest.md's frontmatter updatedAt (or its '# Vault audit - <date>' heading); if it is not today's UTC date, the audit that should have produced it did not run this week. In that case do nothing else -- run 'sh bin/log.sh -k audit \"skipped: audits/latest.md is stale (<that date>)\"' from ~/memory and stop; do not call notify-relay, do not touch any other vault file. Otherwise skip the skill's own step 0 (the audit already ran minutes ago) and go straight through its mechanical-findings, contradiction, rotation-judgment and archive-pass sections. Apply directly everything the skill's own text says to fix/rewrite/archive yourself; anything it says to list under 'Needs Denys' or propose to Denys stays unapplied -- collect those items instead, each as one self-contained line with its quotes/dates/page names (a reader with no other context must be able to act on the line alone). Do not run the memory-defrag skill or any other vault skill; this run is memory-audit only. Hard rules, no exceptions: never edit sources/ content, never edit PLAN.md, never edit a generated block between openclaw markers, bump a page's updatedAt only when you changed its content, never rewrite git history, never commit or push (the host's memory-sync.timer does that on its own schedule). Finish the skill's own way: re-run 'sh bin/audit.sh' to confirm 0 high / 0 medium, then append exactly one log.md line via bin/log.sh in the skill's format ('audit | ...') saying what you changed and archived, or 'nothing to apply' if nothing needed changing -- this line is written every run with a fresh audit, no exceptions. Then the delivery step: build your final Needs Denys list as a JSON array of those one-line strings (an empty array if there is nothing to escalate). Read the previous list from ~/memory/audits/needs-denys-state.json (treat a missing file as an empty array, and never create the file outside this exact path). Compare the two lists as sets, ignoring order. If they are the same set, do not call notify-relay at all. If they differ: when the new list is non-empty, POST one envelope to http://notify-relay:8090/notify (content-type application/json, built with jq so nothing is hand-escaped) with level 'wait', source 'kit', subject 'memory-vault', a headline naming the count (e.g. '3 item(s) need a decision'), body as the joined list (newline-separated, truncate to the shortest form that stays readable if very long), and action 'review ~/memory/audits/latest.md and the memory-vault-cleanup log.md line'; when the new list is empty and the previous one was not, POST instead with level 'good' and a headline like 'needs-Denys list cleared', no body, no action. A curl that fails or a notify-relay response that is not 200 is not fatal -- note it in your final reply, do not retry more than once, and still proceed to the next step. Whether or not you notified, overwrite ~/memory/audits/needs-denys-state.json with the new list (pretty JSON array, trailing newline) so next week's comparison is against what you just computed -- this file is vault state, not a page, so it does not carry frontmatter/updatedAt/log-line rules the way a knowledge page would. End your turn with a short plain-text summary of what you did (applied/archived, the log.md line, whether you notified and at which level) -- this reply is not delivered anywhere itself, it only helps whoever reads this run's transcript."

# Create the cron job iff absent (matched by name).
if docker exec "$GATEWAY" openclaw cron list --all --json 2>/dev/null \
   | jq -e --arg n "$JOB_NAME" 'if type == "array" then . else (.jobs // []) end | any(.name == $n)' >/dev/null; then
  echo "cron '$JOB_NAME' already exists — leaving it untouched"
  echo "(to change schedule/message: openclaw cron edit, or delete + re-run)"
  exit 0
fi

add_out="$(docker exec "$GATEWAY" openclaw cron add \
  --name "$JOB_NAME" \
  --display-name "Memory vault cleanup" \
  --description "Weekly dreaming pass over the vault (README Rule 3): runs the vault's memory-audit skill unattended, applies its 'safe alone' tier, and delivers only its Needs-Denys list through notify-relay, and only on change. Declared by scripts/ensure-memory-vault-cleanup.sh (source: lifekit-stack)." \
  --cron "$CRON_EXPR" --tz "$CRON_TZ" \
  --agent "$AGENT" --session isolated \
  --message "$MESSAGE" \
  --timeout-seconds 1200 \
  --json)"
echo "$add_out"
job_id="$(printf '%s' "$add_out" | jq -r 'first(.. | objects | .id? // empty)' 2>/dev/null || true)"

echo "created cron '$JOB_NAME' ($CRON_EXPR @ $CRON_TZ, agent:$AGENT) — no OpenClaw-level delivery; the agent posts to notify-relay itself, only on a Needs-Denys transition"
if [ -n "$job_id" ]; then
  echo "verify: docker exec $GATEWAY openclaw cron run $job_id --expect-final --json"
  echo "        docker exec $GATEWAY openclaw cron show $job_id"
else
  echo "could not parse job id from cron add output — find it with:" >&2
  echo "  docker exec $GATEWAY openclaw cron list --all --json | jq '.jobs[] | select(.name==\"$JOB_NAME\")'" >&2
fi
