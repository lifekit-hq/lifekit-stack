#!/usr/bin/env bash
# ensure-secondmate-relay.sh — install the secondmate-relay skill
# (skills/secondmate-relay) into kit's workspace, and report whether kit's
# skill allowlist carries it.
#
# The skill is the gateway half of the Kit relay: relay.sh speaks to the
# kit-relay forced command on the host over the read-only key mount at
# /run/lifekit/kit-relay (docs/runbook.md, "Kit's second-mate relay"). Kit's
# allowlist is host state (agents.entries.kit.skills in openclaw.json), so
# this script only reads it; adding the skill there is an operator patch.
#
# Run on the VPS host as lifekit, from the repo checkout:
#   /srv/lifekit-stack/scripts/ensure-secondmate-relay.sh
#
# Re-running converges (both files are overwritten in place). The gateway's
# skill watcher picks up a new SKILL.md; if kit does not see it, force-recreate
# the gateway as in scripts/ensure-morning-brief.sh.
#
# Env overrides:
#   SECONDMATE_RELAY_AGENT  agent whose workspace gets the skill (default: kit)
#   OPENCLAW_CONFIG_DIR     host path of the gateway state dir
#                           (default: /srv/openclaw/config, as in compose)
set -euo pipefail

AGENT="${SECONDMATE_RELAY_AGENT:-kit}"
SKILL=secondmate-relay
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
skill_src="${repo_root}/skills/${SKILL}"
workspace="${OPENCLAW_CONFIG_DIR:-/srv/openclaw/config}/agents/${AGENT}/workspace"
skill_dest="${workspace}/skills/${SKILL}"

[ -d "$workspace" ] || { echo "no workspace for agent '$AGENT' at $workspace" >&2; exit 1; }

install -D -m 0644 "$skill_src/SKILL.md" "$skill_dest/SKILL.md"
install -D -m 0755 "$skill_src/relay.sh" "$skill_dest/relay.sh"
echo "skill installed -> $skill_dest"

# Read-only: is the skill on the agent's allowlist? An agent with a `skills`
# list sees only what the list names.
GATEWAY="$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway 2>/dev/null | head -n 1 || true)"
if [ -z "$GATEWAY" ]; then
  echo "no running openclaw-gateway container; allowlist not checked"
  exit 0
fi
listed="$(docker exec -e AGENT="$AGENT" -e SKILL="$SKILL" "$GATEWAY" node -e '
const c = require(process.env.OPENCLAW_CONFIG_PATH);
const s = ((c.agents || {}).entries || {})[process.env.AGENT]?.skills;
process.stdout.write(!Array.isArray(s) ? "unfiltered" : s.includes(process.env.SKILL) ? "yes" : "no");
' 2>/dev/null || echo unknown)"
case "$listed" in
  yes) echo "allowlist: '$SKILL' is on agent '$AGENT''s skills list" ;;
  unfiltered) echo "allowlist: agent '$AGENT' has no skills list; the skill is visible" ;;
  no) echo "allowlist: '$SKILL' is NOT on agent '$AGENT''s skills list - apply the allowlist patch (docs/runbook.md, \"Kit's second-mate relay\")" ;;
  *) echo "allowlist: could not read agent '$AGENT''s skills list" ;;
esac
