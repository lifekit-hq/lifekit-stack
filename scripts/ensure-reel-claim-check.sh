#!/usr/bin/env bash
# ensure-reel-claim-check.sh - install the reel-claim-check skill
# (skills/reel-claim-check) into kit's workspace, and report whether kit's
# skill allowlist carries it and whether the tools it runs are in the gateway.
#
# The skill downloads an Instagram reel with yt-dlp and transcribes it with the
# whisper and ffmpeg baked into the gateway image (docs/runbook.md, "Instagram
# reel claim check"). It runs the pinned yt-dlp that ensure-youtube-transcript.sh
# installs, so there is one pin to bump. Kit's allowlist (agents.entries.kit.skills)
# is host state, so this script only reads it; adding the skill is an operator patch.
#
# The deploy runs it (deploy-openclaw.sh, post-up phase), so a merge that changes
# the skill or this script installs it. By hand, on the VPS host as lifekit, from
# the repo checkout:
#   /srv/lifekit-stack/scripts/ensure-reel-claim-check.sh
#
# Re-running converges (both files are overwritten in place). The gateway's skill
# watcher picks up a new SKILL.md; if kit does not see it, force-recreate the
# gateway:
#   cd /srv/lifekit-stack/compose/openclaw && docker compose -p openclaw \
#     --env-file /srv/lifekit-secrets/stack.env -f docker-compose.yml \
#     up -d --force-recreate openclaw-gateway
#
# A host with no workspace for the agent (a box without kit) skips with a note
# rather than failing the deploy.
#
# Env overrides:
#   REEL_CLAIM_CHECK_AGENT  agent whose workspace gets the skill (default: kit)
#   OPENCLAW_CONFIG_DIR     host path of the gateway state dir
#                           (default: /srv/openclaw/config, as in compose)
set -euo pipefail

AGENT="${REEL_CLAIM_CHECK_AGENT:-kit}"
SKILL=reel-claim-check
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
skill_src="${repo_root}/skills/${SKILL}"
workspace="${OPENCLAW_CONFIG_DIR:-/srv/openclaw/config}/agents/${AGENT}/workspace"
skill_dest="${workspace}/skills/${SKILL}"

if [ ! -d "$workspace" ]; then
  echo "no workspace for agent '$AGENT' at $workspace; ${SKILL} not installed"
  exit 0
fi

install -D -m 0644 "$skill_src/SKILL.md" "$skill_dest/SKILL.md"
install -D -m 0755 "$skill_src/reel.py" "$skill_dest/reel.py"
echo "skill installed -> $skill_dest"

GATEWAY="$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway 2>/dev/null | head -n 1 || true)"
if [ -z "$GATEWAY" ]; then
  echo "no running openclaw-gateway container; allowlist and tools not checked"
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
  no) echo "allowlist: '$SKILL' is NOT on agent '$AGENT''s skills list - apply the allowlist patch (docs/runbook.md, \"Instagram reel claim check\")" ;;
  *) echo "allowlist: could not read agent '$AGENT''s skills list" ;;
esac

# Read-only: the three tools the skill shells out to, inside the gateway.
# shellcheck disable=SC2016  # expanded by sh inside the container
docker exec "$GATEWAY" sh -c '
for t in ffmpeg whisper; do
  command -v "$t" >/dev/null 2>&1 && echo "tool: $t present" || echo "tool: $t MISSING in the gateway image"
done
[ -f "$HOME/.openclaw/skills/youtube-transcript/yt-dlp" ] \
  && echo "tool: yt-dlp present (youtube-transcript skill)" \
  || echo "tool: yt-dlp MISSING - run scripts/ensure-youtube-transcript.sh"
' 2>/dev/null || echo "tools: could not check inside the gateway"
