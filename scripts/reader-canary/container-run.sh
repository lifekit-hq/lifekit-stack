#!/bin/sh
# Runs INSIDE a throwaway gateway container (see run.sh): applies the shipped reader
# entry, starts the mock model API, and drives one real `openclaw agent --local` turn
# for the reader on the claude-cli runtime. The container has no network, no secrets
# and no live state; /canary is this directory, /out collects the evidence.
set -eu
READER_HOME=/home/node/.openclaw/agents/reader/workspace
DOOR="$READER_HOME/skills/doc-door/door.py"
export PATH="/tmp/canary-bin:$PATH" MOCK_LOG=/out/mock.log CANARY_ARGV_LOG=/out/argv.log \
  MOCK_PORT=18080
mkdir -p /tmp/canary-bin "$READER_HOME/skills/doc-door"
cp /canary/claude /tmp/canary-bin/claude
cp /canary/door_stub.py "$DOOR"
chmod +x "$DOOR"

# The calls the mock model makes: cases.json with the door path filled in.
MOCK_TOOL_CALLS="$(python3 - "$DOOR" <<'PY'
import json, sys
cases = json.load(open("/canary/cases.json"))
print(json.dumps([[c["tool"], {k: v.replace("{door}", sys.argv[1]) if isinstance(v, str) else v for k, v in c["input"].items()}] for c in cases]))
PY
)"
export MOCK_TOOL_CALLS
python3 /canary/mock_anthropic.py 18080 &

# The shipped agent entry, plus only what the sandbox needs: the model's runtime and a
# stub in place of the Workspace connector.
python3 - <<'PY' > /out/patch.json
import json
patch = json.load(open("/agent.json"))
model = patch["agents"]["entries"]["reader"]["model"]
patch["agents"]["defaults"] = {"models": {model: {"agentRuntime": {"id": "claude-cli"}}}}
patch["mcp"] = {"servers": {"google-workspace": {"command": "python3", "args": ["/canary/stub_workspace_mcp.py"]}}}
print(json.dumps(patch))
PY
openclaw config patch --stdin < /out/patch.json > /out/patch.out 2>&1
openclaw approvals allowlist add --agent reader "$DOOR" > /out/allowlist.out 2>&1
openclaw config get agents.entries.reader > /out/entry.json 2>&1 || true
timeout 280 openclaw agent --local --agent reader -m "read the mail" --json > /out/agent.json 2> /out/agent.err || true
