#!/usr/bin/env bash
# ensure-study-panel.sh - converge the `study-panel` agent: the tool-less learning
# agent behind the browser study panel (compose/study-door/ calls it through the
# gateway's /v1/chat/completions, with one forced client function, `reply`).
#
# What the repo owns, and this script installs or checks:
#   - the contract (defaults/agents/study-panel/workspace: AGENTS.md, SOUL.md,
#     IDENTITY.md), overwritten in the live workspace on every run;
#   - the checks that the live agent entry equals the shipped one on every key the
#     cap rests on (allow, deny, exec mode, agentToAgent), has no tools.profile and
#     no skills, has no channel binding, and that no agent can message another
#     (tools.agentToAgent off).
#
# What stays host state: the agent entry itself (agents.entries.study-panel). It is
# the tool cap, so it is applied by hand from defaults/agents/study-panel/agent.json
# after scripts/study-panel-canary/run.sh passes on the host (docs/runbook.md,
# "Study panel and study door"). A box without the entry skips with a note and exits
# 0; with --check it exits 3, so a caller can tell "not installed" from "verified".
#
# The deploy runs it (deploy-openclaw.sh, post-up phase). By hand, on the VPS host
# as lifekit, from the repo checkout:
#   /srv/lifekit-stack/scripts/ensure-study-panel.sh            # converge
#   /srv/lifekit-stack/scripts/ensure-study-panel.sh --check    # read-only: exit 0 only
#                                                               # when the agent is live
#                                                               # and its cap is exact
#
# Env overrides:
#   OPENCLAW_CONFIG_DIR     host path of the gateway state dir
#                           (default: /srv/openclaw/config, as in compose)
set -euo pipefail

AGENT=study-panel
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
contract_src="${repo_root}/defaults/agents/${AGENT}/workspace"
shipped_entry="${repo_root}/defaults/agents/${AGENT}/agent.json"
workspace="${OPENCLAW_CONFIG_DIR:-/srv/openclaw/config}/agents/${AGENT}/workspace"
check_only=0
[ "${1:-}" = "--check" ] && check_only=1

GATEWAY="$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway | head -n 1)"
[ -n "$GATEWAY" ] || { echo "no running openclaw-gateway container in compose project openclaw" >&2; exit 1; }

# The CLI may interleave log lines with its JSON: keep the first JSON value that
# is a dict (key given, or "*" for any dict) or a list (key "[]").
pick_json() { # $1 = required top-level key, "*" or "[]"; stdin = CLI output
  python3 -c '
import json, sys
key, text = sys.argv[1], sys.stdin.read()
dec, i = json.JSONDecoder(), 0
while (i := text.find("[" if key == "[]" else "{", i)) != -1:
    try:
        obj, end = dec.raw_decode(text, i)
    except ValueError:
        i += 1
        continue
    if (key == "[]" and isinstance(obj, list)) or (isinstance(obj, dict) and (key == "*" or key in obj)):
        json.dump(obj, sys.stdout)
        sys.exit(0)
    i = end
sys.exit(1)' "$1"
}

# Only "path not found" / "valid but unset" mean the agent is not installed; any other
# failure of the read is an error, never an absent agent (a live agent must not read as
# absent).
absent_re='not found|valid but unset'
rc=0
got="$(docker exec "$GATEWAY" openclaw config get "agents.entries.${AGENT}" --json 2>&1)" || rc=$?
entry=""
if [ "$rc" = 0 ]; then
  entry="$(printf '%s' "$got" | pick_json '*')" || { echo "could not read agents.entries.${AGENT} from the gateway: ${got}" >&2; exit 1; }
elif ! printf '%s' "$got" | grep -qiE "$absent_re"; then
  echo "could not read agents.entries.${AGENT} from the gateway: ${got}" >&2
  exit 1
fi
if [ -z "$entry" ]; then
  echo "no '${AGENT}' agent in the live config: nothing installed, the study door has no agent to call." >&2
  echo "  apply it (after the canary passes): docs/runbook.md, \"Study panel and study door\"" >&2
  [ "$check_only" = 1 ] && exit 3
  exit 0
fi

# The live entry must be the shipped one on every key the cap rests on. An empty list
# and an absent key are the same cap (the gateway may drop an empty list on write).
printf '%s' "$entry" | python3 -c '
import json, sys
live = json.load(sys.stdin)
want = json.load(open(sys.argv[1]))["agents"]["entries"]["study-panel"]
bad = []
def norm(v):
    if v is None:
        return []
    return sorted(v) if isinstance(v, list) else v
for path in (
    ("workspace",), ("model",), ("sandbox", "mode"), ("tools", "allow"), ("tools", "deny"),
    ("tools", "exec", "mode"), ("tools", "agentToAgent", "send"),
):
    a, b = live, want
    for k in path:
        a = a.get(k) if isinstance(a, dict) else None
        b = b.get(k) if isinstance(b, dict) else None
    if norm(a) != norm(b):
        bad.append(".".join(path) + "=" + repr(a) + ", shipped " + repr(b))
if (live.get("tools") or {}).get("profile") is not None:
    bad.append("tools.profile is set: the cap is allow + deny, with no profile")
if live.get("skills"):
    bad.append("skills is set: the study panel holds no skill")
if bad:
    sys.exit("refusing: the live study-panel entry differs from defaults/agents/study-panel/agent.json: " + "; ".join(bad) + " - re-apply it (docs/runbook.md, \"Study panel and study door\")")
' "$shipped_entry" >&2

# No agent can message another one, and nothing routes a channel to the study panel.
rc=0
a2a="$(docker exec "$GATEWAY" openclaw config get tools.agentToAgent.enabled 2>&1)" || rc=$?
if [ "$rc" != 0 ] && ! printf '%s' "$a2a" | grep -qiE "$absent_re"; then
  echo "could not read tools.agentToAgent.enabled from the gateway: ${a2a}" >&2
  exit 1
fi
if [ "$rc" = 0 ]; then
  case "$(printf '%s' "$a2a" | tr -d '[:space:]"')" in
    *true) echo "refusing: tools.agentToAgent.enabled is true; the study panel must not be reachable by other agents" >&2; exit 1 ;;
  esac
fi
bindings="$(docker exec "$GATEWAY" openclaw agents list --json --bindings 2>/dev/null | pick_json '[]')" || bindings="[]"
printf '%s' "$bindings" | python3 -c '
import json, sys
row = next((a for a in json.load(sys.stdin) if a.get("id") == "study-panel"), None)
if row is None:
    sys.exit("refusing: the study-panel entry is in the config but not in the agent list")
if row.get("bindings"):
    sys.exit("refusing: the study-panel has " + str(row["bindings"]) + " channel binding(s); it is reached only through the study door (openclaw agents unbind --agent study-panel --all)")
' >&2

if [ "$check_only" = 0 ]; then
  for f in AGENTS.md SOUL.md IDENTITY.md; do
    install -D -m 0644 "$contract_src/$f" "$workspace/$f"
  done
  echo "contract installed -> $workspace"
fi

echo "study-panel agent verified: shipped cap live, no bindings, no skills, no agent-to-agent"
echo "verify: scripts/study-panel-canary/run.sh   # the cap, on a managed claude-cli turn and through the HTTP endpoint"
