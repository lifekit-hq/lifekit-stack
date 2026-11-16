#!/usr/bin/env bash
# ensure-firstmate-handoff.sh — install the firstmate-handoff skill
# (skills/firstmate-handoff) into the workspaces of Kit and Ledger, and report
# whether each agent's skill allowlist carries it.
#
# The skill is the agents' route for a dev-class problem: handoff.sh files one
# note to firstmate's inbox over the same kit-relay key and forced command
# secondmate-relay uses (docs/runbook.md, "Dev problems go to firstmate, not the
# owner"). An agent's allowlist is host state (agents.entries.<id>.skills in
# openclaw.json), so this script only reads it; adding the skill there is an
# operator patch, the one in the runbook.
#
# Run on the VPS host as lifekit, from the repo checkout:
#   /srv/lifekit-stack/scripts/ensure-firstmate-handoff.sh
#
# Re-running converges (both files are overwritten in place). The gateway's
# skill watcher picks up a new SKILL.md; if an agent does not see it,
# force-recreate the gateway (see the runbook section above).
#
# Env overrides:
#   FIRSTMATE_HANDOFF_AGENTS  space-separated agent ids (default: "kit finance";
#                             finance is Ledger's agent id)
#   OPENCLAW_CONFIG_DIR       host path of the gateway state dir
#                             (default: /srv/openclaw/config, as in compose)
set -euo pipefail

SKILL=firstmate-handoff
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
skill_src="${repo_root}/skills/${SKILL}"
config_dir="${OPENCLAW_CONFIG_DIR:-/srv/openclaw/config}"
read -r -a agents <<<"${FIRSTMATE_HANDOFF_AGENTS:-kit finance}"

rc=0
for agent in "${agents[@]}"; do
  workspace="${config_dir}/agents/${agent}/workspace"
  if [ ! -d "$workspace" ]; then
    echo "no workspace for agent '$agent' at $workspace" >&2
    rc=1
    continue
  fi
  dest="${workspace}/skills/${SKILL}"
  install -D -m 0644 "$skill_src/SKILL.md" "$dest/SKILL.md"
  install -D -m 0755 "$skill_src/handoff.sh" "$dest/handoff.sh"
  echo "skill installed -> $dest"
done

# Read-only: is the skill on each agent's allowlist? An agent with a `skills`
# list sees only what the list names.
GATEWAY="$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway 2>/dev/null | head -n 1 || true)"
if [ -z "$GATEWAY" ]; then
  echo "no running openclaw-gateway container; allowlists not checked"
  exit "$rc"
fi
for agent in "${agents[@]}"; do
  listed="$(docker exec -e AGENT="$agent" -e SKILL="$SKILL" "$GATEWAY" node -e '
const c = require(process.env.OPENCLAW_CONFIG_PATH);
const s = ((c.agents || {}).entries || {})[process.env.AGENT]?.skills;
process.stdout.write(!Array.isArray(s) ? "unfiltered" : s.includes(process.env.SKILL) ? "yes" : "no");
' 2>/dev/null || echo unknown)"
  case "$listed" in
    yes) echo "allowlist: '$SKILL' is on agent '$agent''s skills list" ;;
    unfiltered) echo "allowlist: agent '$agent' has no skills list; the skill is visible" ;;
    no) echo "allowlist: '$SKILL' is NOT on agent '$agent''s skills list - apply the allowlist patch (docs/runbook.md, \"Dev problems go to firstmate, not the owner\")" ;;
    *) echo "allowlist: could not read agent '$agent''s skills list" ;;
  esac
done
exit "$rc"
