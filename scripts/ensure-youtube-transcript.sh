#!/usr/bin/env bash
# ensure-youtube-transcript.sh - install the youtube-transcript skill
# (skills/youtube-transcript) into the gateway's shared skills directory,
# with a pinned, checksum-verified yt-dlp beside it, and report which agents'
# skill allowlists carry it.
#
# The skill fetches YouTube captions through the SOCKS tunnel scripts/yt-tunnel
# opens to the owner's PC (docs/runbook.md, "YouTube transcripts through the
# PC"). yt-dlp is the upstream python zipapp (python 3.9+; the gateway image has
# 3.11), so no image rebuild and no container recreate is needed: the skill
# directory is a bind mount and the gateway's skill watcher picks the files up.
# Bumping yt-dlp is editing the two literals below and re-running this script.
#
# Shared directory (<state-dir>/skills = ~/.openclaw/skills in the container),
# not one copy per agent: visible to every local agent unless its allowlist
# narrows it. An agent's allowlist is host state (agents.entries.<id>.skills in
# openclaw.json), so this script only reads it; adding the skill there is an
# operator patch.
#
# Run on the VPS host as lifekit, from the repo checkout:
#   /srv/lifekit-stack/scripts/ensure-youtube-transcript.sh
#
# Re-running converges. Env overrides:
#   OPENCLAW_CONFIG_DIR   host path of the gateway state dir
#                         (default: /srv/openclaw/config, as in compose)
set -euo pipefail

YT_DLP_VERSION="2026.08.19"
YT_DLP_SHA256="1fa6733c37ea6fb51c99ad8fe785e7b7e5f3246c9b980230329d4fb72ed8d4d6"

SKILL=youtube-transcript
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
skill_src="${repo_root}/skills/${SKILL}"
state_dir="${OPENCLAW_CONFIG_DIR:-/srv/openclaw/config}"
skill_dest="${state_dir}/skills/${SKILL}"

[ -d "$state_dir" ] || { echo "no gateway state dir at $state_dir" >&2; exit 1; }

install -D -m 0644 "$skill_src/SKILL.md" "$skill_dest/SKILL.md"
install -D -m 0755 "$skill_src/transcript.py" "$skill_dest/transcript.py"

if [ -x "$skill_dest/yt-dlp" ] \
  && echo "${YT_DLP_SHA256}  ${skill_dest}/yt-dlp" | sha256sum -c - >/dev/null 2>&1; then
  echo "yt-dlp ${YT_DLP_VERSION} already in place"
else
  tmp="$(mktemp)"
  trap 'rm -f "$tmp"' EXIT
  curl -fsSL --retry 3 -o "$tmp" \
    "https://github.com/yt-dlp/yt-dlp/releases/download/${YT_DLP_VERSION}/yt-dlp"
  echo "${YT_DLP_SHA256}  ${tmp}" | sha256sum -c - >/dev/null
  install -m 0755 "$tmp" "$skill_dest/yt-dlp"
  echo "yt-dlp ${YT_DLP_VERSION} installed"
fi
echo "skill installed -> $skill_dest"

# Read-only: which agents' allowlists name the skill? An agent with a `skills`
# list sees only what the list names.
GATEWAY="$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway 2>/dev/null | head -n 1 || true)"
if [ -z "$GATEWAY" ]; then
  echo "no running openclaw-gateway container; allowlists not checked"
  exit 0
fi
docker exec -e SKILL="$SKILL" "$GATEWAY" node -e '
const c = require(process.env.OPENCLAW_CONFIG_PATH);
for (const [id, a] of Object.entries((c.agents || {}).entries || {})) {
  const s = a.skills;
  const v = !Array.isArray(s) ? "unfiltered (sees it)" : s.includes(process.env.SKILL) ? "yes" : "NO - apply the allowlist patch (docs/runbook.md)";
  console.log("allowlist: " + id + ": " + v);
}
' 2>/dev/null || echo "allowlist: could not read the agents' skills lists"
