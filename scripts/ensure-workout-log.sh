#!/usr/bin/env bash
# ensure-workout-log.sh — install the workout-log skill (skills/workout-log)
# into kit's workspace, and report whether kit's skill allowlist carries it and
# whether the gym-day nudge cron exists.
#
# The skill logs a session from one line through the workout-claw CLI baked
# into the gateway image (compose/openclaw-gateway/package.json); nudge.sh is
# the command the workout-nudge cron runs (docs/runbook.md, "Kit's workout log
# and gym-day nudge"); day-check.sh is the one the evening-day-plan cron runs
# ("Kit's evening day plan"). Kit's allowlist (agents.entries.kit.skills) and the
# cron, whose day-of-week field holds the gym days, are host state, so this
# script only reads them; adding either is an operator step.
#
# Run on the VPS host as lifekit, from the repo checkout:
#   /srv/lifekit-stack/scripts/ensure-workout-log.sh
#
# Re-running converges (both files are overwritten in place). The gateway's
# skill watcher picks up a new SKILL.md; if kit does not see it, force-recreate
# the gateway:
#   cd /srv/lifekit-stack/compose/openclaw && docker compose -p openclaw \
#     --env-file /srv/lifekit-secrets/stack.env -f docker-compose.yml \
#     up -d --force-recreate openclaw-gateway
#
# Env overrides:
#   WORKOUT_LOG_AGENT    agent whose workspace gets the skill (default: kit)
#   OPENCLAW_CONFIG_DIR  host path of the gateway state dir
#                        (default: /srv/openclaw/config, as in compose)
set -euo pipefail

AGENT="${WORKOUT_LOG_AGENT:-kit}"
SKILL=workout-log
CRON_NAME=workout-nudge
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
skill_src="${repo_root}/skills/${SKILL}"
workspace="${OPENCLAW_CONFIG_DIR:-/srv/openclaw/config}/agents/${AGENT}/workspace"
skill_dest="${workspace}/skills/${SKILL}"

[ -d "$workspace" ] || { echo "no workspace for agent '$AGENT' at $workspace" >&2; exit 1; }

install -D -m 0644 "$skill_src/SKILL.md" "$skill_dest/SKILL.md"
install -D -m 0755 "$skill_src/nudge.sh" "$skill_dest/nudge.sh"
install -D -m 0755 "$skill_src/day-check.sh" "$skill_dest/day-check.sh"
echo "skill installed -> $skill_dest"

GATEWAY="$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway 2>/dev/null | head -n 1 || true)"
if [ -z "$GATEWAY" ]; then
  echo "no running openclaw-gateway container; allowlist and cron not checked"
  exit 0
fi

# Read-only: is the skill on the agent's allowlist? An agent with a `skills`
# list sees only what the list names.
listed="$(docker exec -e AGENT="$AGENT" -e SKILL="$SKILL" "$GATEWAY" node -e '
const c = require(process.env.OPENCLAW_CONFIG_PATH);
const s = ((c.agents || {}).entries || {})[process.env.AGENT]?.skills;
process.stdout.write(!Array.isArray(s) ? "unfiltered" : s.includes(process.env.SKILL) ? "yes" : "no");
' 2>/dev/null || echo unknown)"
case "$listed" in
  yes) echo "allowlist: '$SKILL' is on agent '$AGENT''s skills list" ;;
  unfiltered) echo "allowlist: agent '$AGENT' has no skills list; the skill is visible" ;;
  no) echo "allowlist: '$SKILL' is NOT on agent '$AGENT''s skills list - apply the allowlist patch (docs/runbook.md, \"Kit's workout log and gym-day nudge\")" ;;
  *) echo "allowlist: could not read agent '$AGENT''s skills list" ;;
esac

# Read-only: does the nudge cron exist, and on which days does it fire? The
# JSON is parsed by the container's node, as the host need not have one.
# shellcheck disable=SC2016  # JavaScript template literal, not shell
schedule="$(docker exec "$GATEWAY" openclaw cron list --json 2>/dev/null \
  | docker exec -i -e CRON_NAME="$CRON_NAME" "$GATEWAY" node -e '
let s = "";
process.stdin.on("data", (d) => (s += d)).on("end", () => {
  const j = JSON.parse(s);
  const job = (j.jobs || j).find((x) => x.name === process.env.CRON_NAME);
  process.stdout.write(!job ? "missing" : `${job.schedule?.expr} ${job.schedule?.tz || ""}`.trim());
});
' 2>/dev/null || echo unknown)"
case "$schedule" in
  missing) echo "cron: '$CRON_NAME' is not set up - add it with the gym days (docs/runbook.md, \"Kit's workout log and gym-day nudge\")" ;;
  unknown) echo "cron: could not read the gateway's cron list" ;;
  *) echo "cron: '$CRON_NAME' fires at '$schedule'" ;;
esac
