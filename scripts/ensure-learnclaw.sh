#!/usr/bin/env bash
# ensure-learnclaw.sh - install learnclaw's contract (defaults/agents/learnclaw/
# workspace: AGENTS.md, SOUL.md, IDENTITY.md, skills/README.md) and its
# learning-record skill (skills/learning-record) and LearnHarness's upstream
# learnharness skill into the learnclaw workspace, and report whether the
# agent's skill allowlist carries them.
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

# LearnHarness's own agent skill, adopted as it is (Denys 2026-11-09): the
# read-only reader of the learning record LearnHarness keeps on the owner's PC,
# synced here by scripts/learnharness-sync (docs/runbook.md, "learnclaw").
# Fetched from upstream at a pinned commit and checked file by file, never
# vendored or edited; bump LH_SKILL_COMMIT and the manifest together.
LH_SKILL_COMMIT=713377f366771e3e4730e6acd1bc290e0ebc6d6e # v0.5.0
lh_dest="${workspace}/skills/learnharness"
lh_manifest="0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0  learnharness/LICENSE
0f29a1a5bd9c6f629d76ec442c78799b9d95a83a0322c3a84e71c2c442df09bb  learnharness/SKILL.md
b34f020e9cca38950548c9e21e8cdf86df35d781154bcf2942fe3e323553d926  learnharness/references/data-format.md
fb4abe3f3e99133a9f5a6e28a0733437d1ec37b5e92ba8413102911145cd2883  learnharness/scripts/_data.py
95d05acebef77764a215439a95b39b1bc0a81d2595c67349d90053e548e236ed  learnharness/scripts/graph.py
864368dfde5a8c7550ebf80ec77671d9f159f80c0859b248cce20fe799b95dc7  learnharness/scripts/sessions.py"
if [ -f "$lh_dest/.commit" ] && [ "$(cat "$lh_dest/.commit")" = "$LH_SKILL_COMMIT" ]; then
  echo "learnharness skill at ${LH_SKILL_COMMIT:0:12} -> $lh_dest"
else
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  if curl -fsSL --max-time 60 \
    "https://codeload.github.com/amosblomqvist/learnharness/tar.gz/${LH_SKILL_COMMIT}" \
    | tar -xz -C "$tmp" --strip-components=2 "learnharness-${LH_SKILL_COMMIT}/skills/learnharness" \
    && (cd "$tmp" && printf '%s\n' "$lh_manifest" | sha256sum --quiet -c -) \
    && [ "$(cd "$tmp" && find learnharness -type f | wc -l)" -eq 6 ]; then
    rm -rf "$lh_dest"
    cp -R "$tmp/learnharness" "$lh_dest"
    printf '%s\n' "$LH_SKILL_COMMIT" >"$lh_dest/.commit"
    echo "learnharness skill installed at ${LH_SKILL_COMMIT:0:12} -> $lh_dest"
  else
    echo "learnharness skill NOT installed: fetch or checksum failed for ${LH_SKILL_COMMIT:0:12}; the installed copy (if any) is kept"
  fi
fi

GATEWAY="$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway 2>/dev/null | head -n 1 || true)"
if [ -z "$GATEWAY" ]; then
  echo "no running openclaw-gateway container; allowlist not checked"
  exit 0
fi

# Read-only: is the skill on the agent's allowlist? An agent with a `skills`
# list sees only what the list names; an empty list is the paused state.
for SKILL in learning-record learnharness; do
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
done
