#!/usr/bin/env bash
# ensure-kit-cron-models.sh - pin a Sonnet model on every cron job that runs as
# Kit, so none inherits Kit's interactive Opus default.
#
# Kit's interactive chat runs on anthropic/claude-opus-5-5 (per-agent model in
# the host-state half of openclaw.json). A cron job with no payload.model of
# its own inherits that, at ~2.5x the allowance per token, for turns that
# never needed it (today brief, evening plan note, Sunday review, vault
# cleanup, job-queue drain). Cron rows live in the gateway store, not the
# config file, so the pin is a `cron edit --model` per row.
#
# Usage, on the VPS host as lifekit, from the repo checkout:
#   scripts/ensure-kit-cron-models.sh           # pin every unpinned Kit job
#   scripts/ensure-kit-cron-models.sh --check   # report only; exit 1 if any is unpinned
#
# A job whose payload.kind is "command" runs no model, so it is skipped silently
# (reported as "skipped: command job") in both modes; it still counts as matched.
# Only jobs whose payload.model is EMPTY are touched. A job that already names
# a model (any model) is a deliberate choice and is reported, never rewritten.
# A row naming no agent counts as Kit's (the default agent). Finding no Kit job
# at all is an error, not a pass. Re-running converges. Acceptance: `openclaw cron list` shows a model on every
# Kit job.
#
# Env overrides:
#   KIT_AGENT             the agent id whose jobs are pinned (default: kit)
#   KIT_CRON_MODEL        the model pinned (default: anthropic/claude-sonnet-5-5)
set -euo pipefail

AGENT="${KIT_AGENT:-kit}"
MODEL="${KIT_CRON_MODEL:-anthropic/claude-sonnet-5-5}"
check_only=0
case "${1:-}" in
  --check) check_only=1 ;;
  "") ;;
  *) echo "usage: $0 [--check]" >&2; exit 2 ;;
esac

GATEWAY="$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway 2>/dev/null | head -n 1 || true)"
if [ -z "$GATEWAY" ]; then
  echo "no running openclaw-gateway container; cron models not checked"
  exit 0
fi

jobs_json="$(docker exec "$GATEWAY" openclaw cron list --all --json 2>/dev/null)" || {
  echo "could not read the gateway's cron list" >&2
  exit 1
}

# id<TAB>name<TAB>kind<TAB>model for every job of this agent (kind "-" when unset,
# model empty when unset; tab is whitespace to `read`, so no empty field mid-row).
# A row naming no agent runs as the default agent, so it counts as this one's.
rows="$(printf '%s' "$jobs_json" | jq -r --arg a "$AGENT" '
  (if type == "array" then . else (.jobs // []) end)
  | map(select((.agentId // .agent // $a) == $a))
  | .[] | [.id, (.name // .id), ((.payload.kind // "-") | tostring), ((.payload.model // "") | tostring)] | @tsv')"

matched=0
unpinned=0
failed=0
while IFS=$'\t' read -r id name kind model; do
  [ -n "$id" ] || continue
  matched=$((matched + 1))
  if [ "$kind" = "command" ]; then
    echo "skipped: command job '$name' ($id) runs no model"
    continue
  fi
  if [ -n "$model" ]; then
    echo "ok: '$name' ($id) runs on $model"
    continue
  fi
  unpinned=$((unpinned + 1))
  if [ "$check_only" -eq 1 ]; then
    echo "unpinned: '$name' ($id) inherits ${AGENT}'s default model"
  else
    if docker exec "$GATEWAY" openclaw cron edit "$id" --model "$MODEL" >/dev/null; then
      echo "pinned: '$name' ($id) -> $MODEL"
    else
      echo "failed: could not pin '$name' ($id)" >&2
      failed=$((failed + 1))
    fi
  fi
done <<< "$rows"

if [ "$matched" -eq 0 ]; then
  echo "warning: no ${AGENT} cron jobs matched; the cron list schema may have changed" >&2
  exit 1
fi

if [ "$check_only" -eq 1 ] && [ "$unpinned" -gt 0 ]; then
  echo "$unpinned ${AGENT} job(s) unpinned; run without --check to pin them" >&2
  exit 1
fi
if [ "$failed" -gt 0 ]; then
  echo "$failed ${AGENT} job(s) could not be pinned" >&2
  exit 1
fi
echo "verify: docker exec $GATEWAY openclaw cron list --all --json | jq '.jobs[] | select(.agentId==\"$AGENT\") | [.name, .payload.model]'"
