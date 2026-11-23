#!/bin/sh
# Runs INSIDE a throwaway gateway container (see run.sh): applies the shipped
# study-panel entry, starts the mock model API, and runs two stages on the claude-cli
# runtime. The container has no network, no secrets and no live state; /canary is this
# directory, /out collects the evidence.
#
#   1. LOCAL: one real `openclaw agent --local --agent study-panel` turn whose message is
#      an injection-bearing page. The mock model obeys it and issues every call in
#      cases.json; the verdict wants an empty tool catalogue and every call refused.
#   2. HTTP: the gateway itself, on loopback with the chat-completions endpoint on, gets
#      the request the study door would send (no tools, tool_choice "none"). The mock model
#      answers one JSON object as text. The verdict wants an empty catalogue and the reply
#      to come back as a finished text answer that parses to that object.
set -eu
export PATH="/tmp/canary-bin:$PATH" CANARY_ARGV_LOG=/out/argv.log MOCK_PORT=18080
mkdir -p /tmp/canary-bin
cp /canary/claude /tmp/canary-bin/claude

# The shipped agent entry, plus only what the sandbox needs: the model's runtime, and
# for stage 2 the HTTP endpoint behind a throwaway token.
python3 - <<'PY' > /out/patch.json
import json
patch = json.load(open("/agent.json"))
entries = patch.get("agents", {}).get("entries", {})
model = (entries.get("study-panel") or {}).get("model", "anthropic/claude-sonnet-5-5")
patch.setdefault("agents", {})["defaults"] = {"models": {model: {"agentRuntime": {"id": "claude-cli"}}}}
patch["gateway"] = {
    "mode": "local",
    "auth": {"mode": "token", "token": "canary-token"},
    "http": {"endpoints": {"chatCompletions": {"enabled": True}}},
}
print(json.dumps(patch))
PY
openclaw config patch --stdin < /out/patch.json > /out/patch.out 2>&1
openclaw config get agents.entries.study-panel > /out/entry.json 2>&1 || true

# Stage 1: the local turn.
MOCK_LOG=/out/mock.log MOCK_TOOL_CALLS="$(python3 -c '
import json
print(json.dumps([[c["tool"], c["input"]] for c in json.load(open("/canary/cases.json"))]))')" \
  python3 /canary/mock_anthropic.py 18080 &
MOCK_PID=$!
sleep 1
timeout 280 openclaw agent --local --agent study-panel -m "$(cat /canary/page.txt)" --json > /out/agent.json 2> /out/agent.err || true
kill "$MOCK_PID" 2>/dev/null || true
wait "$MOCK_PID" 2>/dev/null || true

# Stage 2: the gateway's chat-completions endpoint, no tools, one JSON object as text.
MOCK_LOG=/out/mock-http.log MOCK_REPLY_TEXT="$(cat /canary/reply.json)" python3 /canary/mock_anthropic.py 18080 &
MOCK_PID=$!
openclaw gateway run --allow-unconfigured --bind loopback --port 18789 --auth token --token canary-token > /out/gateway.out 2>&1 &
GW_PID=$!
python3 /canary/http_probe.py 18789 canary-token /canary/page.txt /out/http-response.json > /out/http-probe.out 2>&1 || true
kill "$GW_PID" "$MOCK_PID" 2>/dev/null || true
wait 2>/dev/null || true
