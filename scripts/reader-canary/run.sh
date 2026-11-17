#!/usr/bin/env bash
# The reader canary: does the reader's tool cap hold on a managed claude-cli turn?
#
# One real gateway turn (`openclaw agent --local --agent reader`) in a throwaway
# container from the gateway image, with the real claude binary pointed at a mock
# model API that answers with the tool calls in cases.json. The entry applied is the
# shipped defaults/agents/reader/agent.json. The container has no network, no secrets
# and no live state. verdict.py then reads back, per call, whether it was refused or
# ran, and whether anything reached the stub connector or the filesystem.
#
#   scripts/reader-canary/run.sh [image]     (default: lifekit-openclaw:local)
# Exit 0 when every call went the way cases.json says; 77 when docker or the image is
# missing (a skip, not a pass); 1 otherwise.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
image="${1:-lifekit-openclaw:local}"
# CANARY_AGENT_JSON=<file> swaps the entry under test (a negative control: an entry that
# lacks the cap must make this canary fail).
agent_json="${CANARY_AGENT_JSON:-$repo/defaults/agents/reader/agent.json}"
if ! command -v docker >/dev/null 2>&1 || ! docker image inspect "$image" >/dev/null 2>&1; then
  echo "canary: docker or image '$image' not available" >&2
  exit 77
fi
# CANARY_OUT=<dir> keeps the evidence (logs, the turn's output) there instead of a temp dir.
if [ -n "${CANARY_OUT:-}" ]; then
  out="$CANARY_OUT"
  mkdir -p "$out"
else
  out="$(mktemp -d)"
  trap 'rm -rf "$out"' EXIT
fi
chmod 777 "$out"
docker run --rm --network none --memory 3g \
  --tmpfs /home/node/.openclaw:exec,uid=1000,gid=1000,mode=755 \
  --tmpfs /home/node/.claude:uid=1000,gid=1000,mode=755 \
  -v "$here:/canary:ro" -v "$agent_json:/agent.json:ro" \
  -v "$out:/out" --entrypoint sh "$image" /canary/container-run.sh \
  || { echo "canary: container run failed" >&2; exit 1; }
python3 "$here/verdict.py" "$out" "$here/cases.json" "$repo/defaults/agents/reader/agent.json"
