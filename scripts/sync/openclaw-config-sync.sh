#!/usr/bin/env bash
# openclaw-config-sync.sh — mirror VPS openclaw config to lifekit-hq/openclaw-config.
# Pulls /srv/openclaw/config/openclaw.json + per-agent workspace .md files
# into /srv/openclaw-config (a git clone), commits + pushes if changed.
# Run as lifekit (has read access to /srv/openclaw/config/).
set -euo pipefail

REPO=/srv/openclaw-config
LIVE_CONFIG=/srv/openclaw/config/openclaw.json
LIVE_AGENTS_DIR=/srv/openclaw/config/agents

# Workspace top-level files we mirror per agent (MEMORY.md added 2026-05-28).
# Daily notes under workspace/memory/ stay agent-local (.gitignore excludes them).
WORKSPACE_FILES=(AGENTS.md USER.md SOUL.md IDENTITY.md TOOLS.md HEARTBEAT.md MEMORY.md BOOTSTRAP.md)
# Agents whose workspaces we sync.
AGENTS=(kit career finance)

export HOME=/home/lifekit
export GIT_AUTHOR_NAME="Kit (lifekit-vps)"
export GIT_AUTHOR_EMAIL="lifekit-vps@dsdevq.life"
export GIT_COMMITTER_NAME="Kit (lifekit-vps)"
export GIT_COMMITTER_EMAIL="lifekit-vps@dsdevq.life"

cd "$REPO"

# Pull any remote changes first (PC may have pushed). Remote wins on conflict.
git fetch origin --quiet
git reset --hard origin/main --quiet

# Mirror live config into the clone.
cp "$LIVE_CONFIG" openclaw.json

for agent in "${AGENTS[@]}"; do
  src="$LIVE_AGENTS_DIR/$agent/workspace"
  [ -d "$src" ] || continue
  dest="agents/$agent/workspace"
  mkdir -p "$dest"
  for f in "${WORKSPACE_FILES[@]}"; do
    [ -f "$src/$f" ] && cp "$src/$f" "$dest/$f"
  done
done

# Commit + push if anything changed.
if [[ -n "$(git status --porcelain)" ]]; then
  git add -A
  git commit -m "auto: lifekit-vps openclaw sync $(date -u +%FT%TZ)" --quiet
  git push origin main --quiet
fi
