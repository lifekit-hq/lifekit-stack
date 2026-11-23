#!/usr/bin/env bash
# The study-panel canary: does the tool-less study-panel cap hold, and does the one
# client function the study door sends still work with it?
#
# Two stages in one throwaway container from the gateway image (container-run.sh): a
# real local turn (`openclaw agent --local --agent study-panel`) against a mock model
# that obeys an injection-bearing page and calls every tool it can think of, and the
# gateway's own /v1/chat/completions with the door's request (one function, `reply`,
# pinned). The real claude binary is pointed at the mock model API. The entry applied is
# the shipped defaults/agents/study-panel/agent.json. The container has no network, no
# secrets and no live state. verdict.py reads back what was offered and what each call
# got.
#
#   scripts/study-panel-canary/run.sh [image]     (default: lifekit-openclaw:local)
# Exit 0 when the cap held and the reply came back as a structured call; 77 when docker
# or the image is missing (a skip, not a pass); 1 otherwise.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
image="${1:-lifekit-openclaw:local}"
# CANARY_AGENT_JSON=<file> swaps the entry under test (a negative control: an entry that
# lacks the cap, e.g. one with no `tools` block, must make this canary fail). The verdict
# always judges against the shipped entry's allow-list.
agent_json="${CANARY_AGENT_JSON:-$repo/defaults/agents/study-panel/agent.json}"
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
python3 "$here/verdict.py" "$out" "$here/cases.json" "$repo/defaults/agents/study-panel/agent.json" "$here/reply.json"
