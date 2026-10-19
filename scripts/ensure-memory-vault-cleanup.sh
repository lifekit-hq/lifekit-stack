#!/usr/bin/env bash
# ensure-memory-vault-cleanup.sh — idempotently declare the weekly
# memory-vault-cleanup cron: the vault's model pass. It runs the model jobs of
# the vault design (M1 distill an oversized page, M2 merge restatements, M3
# resolve contradictions, M4 distill before rotate, M6 prepare owner calls)
# on the findings the weekly memory_vault_audit just wrote, and nothing else.
# Deterministic work (lint, index, rotation, archive moves) stays in the
# vault's scripts; the model only does what needs judgment.
#
# Why a script and not config: cron jobs live in the gateway's SQLite state,
# not in openclaw.json — same split as ensure-finance-pulse.sh. This script IS the git-side declaration of the
# job: re-running converges it (an existing job gets this message,
# description and delivery mode; schedule and timeout are left alone).
#
# Why an agentTurn on the existing kit agent: the vault (/srv/memory) is
# bind-mounted read-write into the gateway at /home/node/memory
# (compose/openclaw/docker-compose.yml), and kit already owns the command-kind
# memory_vault_audit cron. Nothing is installed into an agent workspace.
#
# Input is a findings list, never "look around": audits/findings.json (the
# lint findings, by rule) plus the contradiction-candidate and Rotation
# sections of audits/latest.md. Findings outside M1-M4 (log-line-too-long,
# say) are left for the scripts or a separate session.
#
# Its own author: every edit is a git commit by `memory-model` with a
# `model:` subject, so the pass is listed apart from the `auto:` sync commits
# and can be reverted. Each finding is its own gate: it lints with
# `bin/lint.sh --changed --tsv`, looks only at the rows for the files it
# changed, and is all-or-nothing per finding: if any of them stays red it
# restores every file the finding touched and names the finding as skipped,
# otherwise it commits them together with
# `git commit --no-verify -- <its paths>`. --no-verify because the vault hook
# lints every changed file on disk, including the red ones memory-sync leaves
# uncommitted, which would refuse the commit for files the job never touched;
# no edit is left on disk for the sync to sweep into an `auto:` commit. When
# bin/log.sh reports its entry uncommitted (the same hook refuses it), the job
# commits that log file itself the same way, as `model: log ...`. A job checks
# `git status --porcelain -- <path>` before editing a file: an already-dirty
# path holds someone else's uncommitted edits (a red file memory-sync kept), so
# the job skips that finding this week, never restores or commits the file, and
# names it in its closing log line; the next week's run retries it.
# Nothing is pushed here — scripts/sync/memory-sync.sh pushes them on its next
# tick. While the pass runs it holds ~/memory/.model-pass,
# and memory-sync skips its ticks while that file is under an hour old, so a
# half-done edit never lands in an `auto:` commit.
#
# Owner calls (M6) go through notify-relay, not `fleet-policy decide`:
# fleet-policy lives in the maintainer's home on the host, which neither the
# gateway container nor the lifekit account can reach. Every owner-facing
# notification in this stack speaks one grammar through one renderer
# (docs/message-format.md); notify-relay is reachable from the gateway at
# http://notify-relay:8090/notify with no chat id needed. Delivery is
# transition-only: the prompt keeps the previous list in
# audits/needs-denys-state.json and posts only when the set changes.
#
# --no-deliver is required, not optional: without it, OpenClaw's own default
# delivery still applies to the job (channel "last", which resolves to
# nothing for an isolated-session job with no prior chat — confirmed live,
# 2026-09-29: cron show reported delivery mode "announce"/channel "last" and
# a deliveryPreview that fail-closes every run: "last -> no route, ...
# requires target <chatId>"). skill-collection-review-kit, the other silent
# agent-turn job on this agent, carries delivery mode "none" for the same
# reason.
#
# Why 20 minutes after memory_vault_audit (host state: "30 3 * * 0"
# Europe/Dublin — see scripts/memory-audit/README.md): the audit itself
# finishes in well under a minute (observed ~38s on this host). The offset
# is buffer, not a measured dependency — the cron store has no cross-job
# "wait for" primitive, so the two jobs are ordered by clock offset alone.
# The prompt checks the findings' date first, so a slow or failed audit run
# is a no-op here, not a race. The audit rotates before this pass runs, so M4
# reads the entries rotation has already moved (see docs/runbook.md).
#
# Run on the VPS host as lifekit, from the repo checkout:
#   /srv/lifekit-stack/scripts/ensure-memory-vault-cleanup.sh
#
# Env overrides:
#   MEMORY_VAULT_CLEANUP_CRON  cron expression (default: "50 3 * * 0")
#   MEMORY_VAULT_CLEANUP_TZ    IANA tz (default: Europe/Dublin — matches
#                               memory_vault_audit)
set -euo pipefail

# The gateway container of the `openclaw` compose project, found by its
# compose labels rather than a fixed container name.
GATEWAY="$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway | head -n 1)"
[ -n "$GATEWAY" ] || { echo "no running openclaw-gateway container in compose project openclaw" >&2; exit 1; }
JOB_NAME=memory-vault-cleanup
AGENT=kit
CRON_EXPR="${MEMORY_VAULT_CLEANUP_CRON:-50 3 * * 0}"
CRON_TZ="${MEMORY_VAULT_CLEANUP_TZ:-Europe/Dublin}"

DESCRIPTION="Weekly model pass over the vault: model jobs M1-M4 and M6 on the findings memory_vault_audit just wrote, committed as memory-model with model: subjects; owner calls go through notify-relay, only on change. Declared by scripts/ensure-memory-vault-cleanup.sh (source: lifekit-stack)."

MESSAGE="$(cat <<'MSG'
Weekly memory-vault model pass: the model jobs M1-M4 and M6. The vault is mounted at ~/memory; run every command from there.

Input is the script findings the weekly audit just wrote, never a look around the vault: ~/memory/audits/findings.json ({date, findings: [{severity, rule, path, detail}]}) and two sections of ~/memory/audits/latest.md, the contradiction candidates and "Rotation (README Rule 3)". Work only on what those name. Read ~/memory/README.md for the page rules before editing.

Precondition: if findings.json's date is not today's UTC date, the audit did not run this week. Run 'sh bin/log.sh -k audit "skipped: audits/findings.json is stale (<its date>)"' and stop; touch nothing else and notify nothing.

Setup and author. First run 'touch .model-pass'; the host's sync skips its ticks while that file is fresh. Your very last step, also when you stop early or a step fails, is 'rm -f .model-pass'. Prefix every git commit and every bin/log.sh call with GIT_AUTHOR_NAME=memory-model GIT_AUTHOR_EMAIL=memory-model@lifekit.invalid GIT_COMMITTER_NAME=memory-model GIT_COMMITTER_EMAIL=memory-model@lifekit.invalid. Commit each finding as soon as it is done, with its own all-or-nothing gate: run 'sh bin/lint.sh --changed --tsv' and read only the rows (severity, rule, path, detail) whose path is one of the files that finding changed. Red files the sync left on disk are not yours and do not block you. If any of the finding's files has a high or medium row, fix it once; if any is still red, restore every file that finding touched, only your own edits (git checkout -- <path> for a tracked file, rm for a file you created), and name the finding as skipped in your closing log line; a facts-moving split must never land half. When all of its files lint clean, commit them together in one commit, naming only those paths: 'git add -- <paths>' then 'git commit --no-verify -m "model: M<n> <what changed, one line>" -- <paths>'. --no-verify is for this gate only: the vault hook would lint the sync's red files too. Never push, never amend or rewrite history. Never leave any file you changed uncommitted on disk: it is either in a model: commit or restored. bin/log.sh commits its own entry; if it reports that the entry is uncommitted, commit that log file yourself with 'git commit --no-verify -m "model: log <what>" -- <log file>' under the same author, in every place you call it. A log file you appended to for a job counts among that job's paths. Before a job edits any file, run 'git status --porcelain -- <path>'. If the path is already dirty, those are someone else's uncommitted edits (a red file the sync left on disk): leave that finding alone this week, never edit, restore or commit that file, and name the skipped finding in your closing log line. It is retried next week. This is not an M6 owner call.

Jobs, in this order:
- M1, every page-too-long finding: split the page by subject into the pages that own those subjects, keeping every fact; dated narrative goes to the page's log through bin/log.sh.
- M2, every restated-subject finding: replace the restating lines with a link to the owning page the finding names. If the copy carries a newer fact, update the owner first.
- M3, every SUSPECT line among the contradiction candidates: the newest dated evidence wins; fix the losing page to agree with it or link to it. When both sides are the owner's own rulings and no newer evidence settles it, change nothing and make it an M6 call.
- M4, every log item in the Rotation section: read the entries it moved out, where the item says they went, and lift any lasting fact still missing from a page into that page. Never move or delete log lines yourself.
- M6, owner calls: one self-contained line each, with the quotes, dates and page names a reader needs to act on it alone, ending with your recommendation. They are the M3 cases you could not settle and every finding a hard rule below stops you from fixing.

Hard rules: never edit sources/, PLAN.md, audits/ (except the state file below), the index or any generated block between openclaw markers; such a finding becomes an M6 call. Bump a page's updatedAt only when you changed its content. Do not run any vault skill. Leave findings of other rules alone.

Finish: run 'sh bin/lint.sh' and confirm none of the pages you edited carries a high or medium finding. Then append exactly one line with bin/log.sh -k audit, every run: 'model pass: M1 <n>, M2 <n>, M3 <n>, M4 <n>, <n> owner call(s)', or 'model pass: nothing to apply'; when you skipped findings, add '; skipped: <rule> <path>, ...' (shortened to fit the log line cap).

Delivery of the M6 list: build it as a JSON array of those lines (empty when there are none). Read the previous list from audits/needs-denys-state.json (a missing file is an empty array). Compare the two as sets. If they are the same, do not call notify-relay. If they differ and the new list is not empty, POST one envelope to http://notify-relay:8090/notify (content-type application/json, built with jq) with level "wait", source "kit", subject "memory-vault", a headline naming the count (e.g. "3 owner call(s) from the vault model pass"), body the lines joined by newlines (shortened if very long), and action "review ~/memory/audits/needs-denys-state.json". If the new list is empty and the previous one was not, POST level "good" with a headline like "vault owner calls cleared", no body, no action. A failed curl or a non-200 reply is not fatal: retry once at most, note it in your reply, go on. Then write the new list to audits/needs-denys-state.json (pretty JSON array, trailing newline) and commit it as 'model: M6 owner calls'.

End with 'rm -f .model-pass' and a short plain-text summary: the commits you made, the log line, and whether you notified and at which level. The reply is not delivered anywhere.
MSG
)"

# Converge, don't just skip, when the job already exists: this message,
# description and delivery mode replace whatever an older run of this script
# (or a manual edit) left; schedule and timeout stay as they are.
existing_id="$(docker exec "$GATEWAY" openclaw cron list --all --json 2>/dev/null \
  | jq -r --arg n "$JOB_NAME" '(if type == "array" then . else (.jobs // []) end) | map(select(.name == $n)) | first | .id // empty')"

if [ -n "$existing_id" ]; then
  docker exec "$GATEWAY" openclaw cron edit "$existing_id" \
    --description "$DESCRIPTION" --message "$MESSAGE" --no-deliver >/dev/null
  echo "cron '$JOB_NAME' already existed (id $existing_id) — converged its message, description and delivery (--no-deliver); schedule left untouched"
  echo "verify: docker exec $GATEWAY openclaw cron show $existing_id   # delivery.mode should read \"none\""
  exit 0
fi

add_out="$(docker exec "$GATEWAY" openclaw cron add \
  --name "$JOB_NAME" \
  --display-name "Memory vault cleanup" \
  --description "$DESCRIPTION" \
  --cron "$CRON_EXPR" --tz "$CRON_TZ" \
  --agent "$AGENT" --session isolated \
  --message "$MESSAGE" \
  --timeout-seconds 1200 \
  --no-deliver \
  --json)"
echo "$add_out"
job_id="$(printf '%s' "$add_out" | jq -r 'first(.. | objects | .id? // empty)' 2>/dev/null || true)"

echo "created cron '$JOB_NAME' ($CRON_EXPR @ $CRON_TZ, agent:$AGENT) — delivery mode none; the agent posts to notify-relay itself, only when its owner-call list changes"
if [ -n "$job_id" ]; then
  echo "verify: docker exec $GATEWAY openclaw cron show $job_id   # delivery.mode should read \"none\""
  echo "        docker exec $GATEWAY openclaw cron run $job_id --expect-final --json"
else
  echo "could not parse job id from cron add output — find it with:" >&2
  echo "  docker exec $GATEWAY openclaw cron list --all --json | jq '.jobs[] | select(.name==\"$JOB_NAME\")'" >&2
fi
