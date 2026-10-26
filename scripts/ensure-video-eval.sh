#!/usr/bin/env bash
# ensure-video-eval.sh - install the video-eval skill (skills/video-eval) into
# kit's workspace, retire the reel-claim-check skill it replaced, and report
# whether kit's skill allowlist carries it and whether the tools it runs are in
# the gateway.
#
# The skill queues a video (a YouTube or Instagram link, or a file already on
# the server) on the shared job queue (skills/job-queue, installed by
# ensure-job-queue.sh); the drain runs it, fetching through the queue's egress
# with the pinned yt-dlp and the media code (media.py: captions, downloads,
# whisper) of the youtube-transcript skill (ensure-youtube-transcript.sh, one
# pin to bump), and the whisper and ffmpeg baked into the gateway image
# transcribe what has no captions. Kit judges it
# against skills/video-eval/rubric.md (docs/runbook.md, "Video evaluation").
# Kit's allowlist (agents.entries.kit.skills) is host state, so this script only
# reads it; adding the skill is an operator patch.
#
# The deploy runs it (deploy-openclaw.sh, post-up phase), so a merge that changes
# the skill or this script installs it. By hand, on the VPS host as lifekit, from
# the repo checkout:
#   /srv/lifekit-stack/scripts/ensure-video-eval.sh
#
# Re-running converges (every file is overwritten in place). The gateway's skill
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
#   VIDEO_EVAL_AGENT        agent whose workspace gets the skill (default: kit)
#   OPENCLAW_CONFIG_DIR     host path of the gateway state dir
#                           (default: /srv/openclaw/config, as in compose)
set -euo pipefail

AGENT="${VIDEO_EVAL_AGENT:-kit}"
SKILL=video-eval
OLD_SKILL=reel-claim-check
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
skill_src="${repo_root}/skills/${SKILL}"
workspace="${OPENCLAW_CONFIG_DIR:-/srv/openclaw/config}/agents/${AGENT}/workspace"
skill_dest="${workspace}/skills/${SKILL}"

if [ ! -d "$workspace" ]; then
  echo "no workspace for agent '$AGENT' at $workspace; ${SKILL} not installed"
  exit 0
fi

install -D -m 0644 "$skill_src/SKILL.md" "$skill_dest/SKILL.md"
install -D -m 0755 "$skill_src/video.py" "$skill_dest/video.py"
install -D -m 0644 "$skill_src/rubric.md" "$skill_dest/rubric.md"
echo "skill installed -> $skill_dest"

# The skill this one replaced, as an earlier deploy installed it: only its two
# files, so a directory holding anything else is left for the operator.
old_dest="${workspace}/skills/${OLD_SKILL}"
if [ -d "$old_dest" ]; then
  rm -f "$old_dest/SKILL.md" "$old_dest/reel.py"
  if rmdir "$old_dest" 2>/dev/null; then
    echo "retired skill removed -> $old_dest"
  else
    echo "retired skill: $old_dest still holds other files; left in place"
  fi
fi

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
  no) echo "allowlist: '$SKILL' is NOT on agent '$AGENT''s skills list - apply the allowlist patch (docs/runbook.md, \"Video evaluation\")" ;;
  *) echo "allowlist: could not read agent '$AGENT''s skills list" ;;
esac

# Read-only: the tools the skill shells out to, the modules it imports, and the
# owner's goals file, inside the gateway.
# shellcheck disable=SC2016  # expanded by sh inside the container
docker exec "$GATEWAY" sh -c '
for t in ffmpeg whisper; do
  command -v "$t" >/dev/null 2>&1 && echo "tool: $t present" || echo "tool: $t MISSING in the gateway image"
done
yt="$HOME/.openclaw/skills/youtube-transcript"
[ -f "$yt/yt-dlp" ] && [ -f "$yt/media.py" ] \
  && echo "tool: yt-dlp and media.py present (youtube-transcript skill)" \
  || echo "tool: yt-dlp or media.py MISSING - run scripts/ensure-youtube-transcript.sh"
[ -f "$HOME/.openclaw/skills/job-queue/jobq.py" ] \
  && echo "tool: jobq.py present (job-queue skill)" \
  || echo "tool: jobq.py MISSING - run scripts/ensure-job-queue.sh"
[ -s "$HOME/.life-state/intake/goals.md" ] \
  && echo "goals: on file" \
  || echo "goals: none on file - the judge says so; write /var/lib/lifekit/intake/goals.md (docs/runbook.md, \"Video evaluation\")"
' 2>/dev/null || echo "tools: could not check inside the gateway"
