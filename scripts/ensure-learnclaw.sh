#!/usr/bin/env bash
# ensure-learnclaw.sh - install learnclaw's contract (defaults/agents/learnclaw/
# workspace: AGENTS.md, SOUL.md, IDENTITY.md, skills/README.md) and its
# learning-record skill (skills/learning-record) into the learnclaw workspace,
# and report whether the agent's skill allowlist carries the skill.
#
# learnclaw owns the learning loop: it reads and appends the artefact record on
# the vault's learning page through the skill, and nothing else writes there
# (docs/runbook.md, "learnclaw (the learning loop)"). Its persona changes only
# through this repo, so the deploy overwrites the live contract files with the
# git ones; edit them here, never under /srv/openclaw. The agent entry and its
# allowlist (agents.entries.learnclaw) are host state, so this script only reads
# them; creating the agent is an operator step.
#
# The deploy runs it (deploy-openclaw.sh, post-up phase), so a merge that changes
# the contract, the skill or this script installs it. By hand, on the VPS host as
# lifekit, from the repo checkout:
#   /srv/lifekit-stack/scripts/ensure-learnclaw.sh
#
# Re-running converges (every file is overwritten in place). A host with no
# learnclaw workspace (the agent not created yet, or retired) skips with a note
# rather than failing the deploy.
#
# Env overrides:
#   OPENCLAW_CONFIG_DIR     host path of the gateway state dir
#                           (default: /srv/openclaw/config, as in compose)
set -euo pipefail

AGENT=learnclaw
SKILL=learning-record
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
contract_src="${repo_root}/defaults/agents/${AGENT}/workspace"
skill_src="${repo_root}/skills/${SKILL}"
workspace="${OPENCLAW_CONFIG_DIR:-/srv/openclaw/config}/agents/${AGENT}/workspace"
skill_dest="${workspace}/skills/${SKILL}"

if [ ! -d "$workspace" ]; then
  echo "no workspace for agent '$AGENT' at $workspace; contract and ${SKILL} not installed"
  exit 0
fi

for f in AGENTS.md SOUL.md IDENTITY.md skills/README.md; do
  install -D -m 0644 "$contract_src/$f" "$workspace/$f"
done
echo "contract installed -> $workspace"

install -D -m 0644 "$skill_src/SKILL.md" "$skill_dest/SKILL.md"
install -D -m 0755 "$skill_src/record.py" "$skill_dest/record.py"
echo "skill installed -> $skill_dest"

GATEWAY="$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway 2>/dev/null | head -n 1 || true)"
if [ -z "$GATEWAY" ]; then
  echo "no running openclaw-gateway container; allowlist not checked"
  exit 0
fi

# Read-only: is the skill on the agent's allowlist? An agent with a `skills`
# list sees only what the list names; an empty list is the paused state.
listed="$(docker exec -e AGENT="$AGENT" -e SKILL="$SKILL" "$GATEWAY" node -e '
const c = require(process.env.OPENCLAW_CONFIG_PATH);
const e = ((c.agents || {}).entries || {})[process.env.AGENT];
const s = e?.skills;
process.stdout.write(!e ? "noentry" : !Array.isArray(s) ? "unfiltered" : s.includes(process.env.SKILL) ? "yes" : "no");
' 2>/dev/null || echo unknown)"
case "$listed" in
  yes) echo "allowlist: '$SKILL' is on agent '$AGENT''s skills list" ;;
  unfiltered) echo "allowlist: agent '$AGENT' has no skills list; the skill is visible" ;;
  no) echo "allowlist: '$SKILL' is NOT on agent '$AGENT''s skills list - paused, or apply the entry patch (docs/runbook.md, \"learnclaw (the learning loop)\")" ;;
  noentry) echo "allowlist: no agent entry '$AGENT' in the live config - create it (docs/runbook.md, \"learnclaw (the learning loop)\")" ;;
  *) echo "allowlist: could not read agent '$AGENT''s skills list" ;;
esac
