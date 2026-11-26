#!/usr/bin/env bash
# ensure-reader-agent.sh - converge the `reader` agent: the one agent that reads
# untrusted text (the needs-you mail reader and the document door run as it).
#
# What the repo owns, and this script installs or checks:
#   - the contract (defaults/agents/reader/workspace: AGENTS.md, SOUL.md,
#     IDENTITY.md, skills/README.md), overwritten in the live workspace on every run;
#   - the exec allowlist: the reader's one allowed command is the door script, by
#     its path (`openclaw approvals allowlist add --agent reader`, idempotent), and
#     the script fails if the reader's allowlist holds anything else;
#   - the checks that the live agent entry equals the shipped one, has no channel
#     binding, and that no agent can reach it: agent-to-agent off, or on with
#     tools.agentToAgent.allow naming every agent the repo defines that may message
#     (each defaults/agents/<id>/workspace, and Ledger's workspace id from
#     scripts/ledger-persona, minus the ones whose agent.json ships send []), and
#     each installed agent's tools.agentToAgent.send equal to the one its
#     defaults/agents/<id>/agent.json ships; the script converges both when it is on
#     and --check requires them, and never names the reader.
#
# What stays host state: the agent entry itself (agents.entries.reader). It is the
# tool cap, so it is applied by hand from defaults/agents/reader/agent.json after
# scripts/reader-canary/run.sh passes on the host (docs/runbook.md, "Reader agent").
# A box without the entry skips with a note and exits 0, which is also how the two
# crons stay where they are until the operator has applied it: ensure-doc-door.sh and
# ensure-needs-you-digest.sh move their jobs only once this script's check passes
# (they call it with --check).
#
# The deploy runs it (deploy-openclaw.sh, post-up phase, before the two scripts
# above). By hand, on the VPS host as lifekit, from the repo checkout:
#   /srv/lifekit-stack/scripts/ensure-reader-agent.sh            # converge
#   /srv/lifekit-stack/scripts/ensure-reader-agent.sh --check    # read-only: exit 0 only
#                                                                # when the agent is live
#                                                                # and its cap is exact
#
# Env overrides:
#   OPENCLAW_CONFIG_DIR     host path of the gateway state dir
#                           (default: /srv/openclaw/config, as in compose)
set -euo pipefail

AGENT=reader
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
contract_src="${repo_root}/defaults/agents/${AGENT}/workspace"
shipped_entry="${repo_root}/defaults/agents/${AGENT}/agent.json"
workspace="${OPENCLAW_CONFIG_DIR:-/srv/openclaw/config}/agents/${AGENT}/workspace"
door=/home/node/.openclaw/agents/${AGENT}/workspace/skills/doc-door/door.py
check_only=0
[ "${1:-}" = "--check" ] && check_only=1

GATEWAY="$(docker ps -q --filter label=com.docker.compose.project=openclaw \
  --filter label=com.docker.compose.service=openclaw-gateway | head -n 1)"
[ -n "$GATEWAY" ] || { echo "no running openclaw-gateway container in compose project openclaw" >&2; exit 1; }

# The CLI may interleave log lines with its JSON: keep the first JSON value that
# is a dict (key given) or a list (key "[]").
pick_json() { # $1 = required top-level key, or "[]" for a list; stdin = CLI output
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

# Only "path not found" / "valid but unset" (OpenClaw 2026.11.4+) mean the agent is not
# installed; any other failure of the read is an error, never an absent reader (a live
# reader must not read as absent).
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
  echo "no '${AGENT}' agent in the live config: nothing installed, its jobs stay where they are." >&2
  echo "  apply it (after the canary passes): docs/runbook.md, \"Reader agent\"" >&2
  [ "$check_only" = 1 ] && exit 3
  exit 0
fi

# The live entry must be the shipped one on every key the cap rests on.
printf '%s' "$entry" | python3 -c '
import json, sys
live = json.load(sys.stdin)
want = json.load(open(sys.argv[1]))["agents"]["entries"]["reader"]
bad = []
def norm(v):
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
if bad:
    sys.exit("refusing: the live reader entry differs from defaults/agents/reader/agent.json: " + "; ".join(bad) + " - re-apply it (docs/runbook.md, \"Reader agent\")")
' "$shipped_entry" >&2

# What the reader can receive: agent-to-agent stays on so every agent can reach Kit,
# and nothing reaches the reader. Both the requester and the target must match the
# global tools.agentToAgent.allow (OpenClaw 2026.11.4 schema), and an agent's own
# tools.agentToAgent.send only narrows its outbound sends inside it, so there is no
# one-way rule: the reader stays out of the list, which keeps it unreachable and keeps
# it from sending. The ids and each agent's send come from the repo's agent
# definitions, never live config values: every defaults/agents/<id> that has a
# workspace (the shared/ snippets are not an agent) and Ledger's id from its persona
# installer's default workspace (scripts/ledger-persona), each with the send list its
# defaults/agents/<id>/agent.json ships. An agent that ships send [] (the reader, the
# study panel) is isolated and stays out of the allow list; every other one is in it
# and must ship a send list (Kit ["*"], every other agent ["kit"], so an agent reaches
# Kit and no other agent). A new definition joins on the next deploy without an edit
# here. Disabled is accepted too. Enabled with an absent or empty allow list means
# every agent is reachable: refused.
derive_a2a() { # stdout = {"allow": [ids, sorted], "send": {id: [targets]}}, never the reader
  python3 - "$repo_root" "$AGENT" <<'PY'
import json, re, sys
from pathlib import Path
root, reader = Path(sys.argv[1]), sys.argv[2]
agents = root / "defaults/agents"
ids = {d.name for d in agents.iterdir() if (d / "workspace").is_dir()}
ledger = (root / "scripts/ledger-persona/ledger-persona.py").read_text()
m = re.search(r'^DEFAULT_WORKSPACE = "[^"]*/agents/([^/"]+)/workspace"$', ledger, re.M)
if not m:
    sys.exit("could not find Ledger's agent id (DEFAULT_WORKSPACE) in scripts/ledger-persona/ledger-persona.py")
ids.add(m.group(1))
ids.discard(reader)
send = {}
for i in sorted(ids):
    f = agents / i / "agent.json"
    s = json.loads(f.read_text()) if f.is_file() else {}
    for k in ("agents", "entries", i, "tools", "agentToAgent", "send"):
        s = s.get(k) if isinstance(s, dict) else None
    if not isinstance(s, list):
        sys.exit("refusing: defaults/agents/" + i + "/agent.json ships no tools.agentToAgent.send list; every agent states whom it may message")
    if s:
        send[i] = s
if not send or any(not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", i) for i in send):
    sys.exit("refusing: the derived agent-to-agent list " + repr(sorted(send)) + " is empty or holds an id that is not a plain agent id")
json.dump({"allow": sorted(send), "send": send}, sys.stdout)
PY
}
a2a="$(derive_a2a)"
read_cfg() { # $1 = config path; sets rc and got ("" when unset)
  rc=0
  got="$(docker exec "$GATEWAY" openclaw config get "$1" --json 2>&1)" || rc=$?
  if [ "$rc" != 0 ] && ! printf '%s' "$got" | grep -qiE "$absent_re"; then
    echo "could not read $1 from the gateway: ${got}" >&2
    exit 1
  fi
  [ "$rc" = 0 ] || got=""
  return 0
}
read_a2a_live() { # sets live_allow (null when unset) and live_entries
  read_cfg tools.agentToAgent.allow
  live_allow="$(printf '%s' "$got" | pick_json '[]')" || live_allow=null
  read_cfg agents.entries
  live_entries="$(printf '%s' "$got" | pick_json '*')" || live_entries="{}"
}
# $1 = patch: print the config patch that converges the allow list and each live
# agent's send (nothing when both already match); check: refuse any difference. An
# agent with no live entry is not installed on this box and gets no entry from here.
a2a_compare() {
  A2A="$a2a" LIVE_ALLOW="$live_allow" LIVE_ENTRIES="$live_entries" python3 -c '
import json, os, sys
mode, want = sys.argv[1], json.loads(os.environ["A2A"])
allow, entries = json.loads(os.environ["LIVE_ALLOW"]), json.loads(os.environ["LIVE_ENTRIES"])
drift = {}
for i, s in want["send"].items():
    e = entries.get(i)
    if not isinstance(e, dict):
        continue
    have = ((e.get("tools") or {}).get("agentToAgent") or {}).get("send")
    if not isinstance(have, list) or sorted(have) != sorted(s):
        drift[i] = (have, s)
allow_ok = isinstance(allow, list) and sorted(allow) == want["allow"]
if mode == "patch":
    patch = {}
    if not allow_ok:
        patch["tools"] = {"agentToAgent": {"allow": want["allow"]}}
    if drift:
        patch["agents"] = {"entries": {i: {"tools": {"agentToAgent": {"send": s}}} for i, (_, s) in drift.items()}}
    if patch:
        json.dump(patch, sys.stdout)
    sys.exit(0)
if not isinstance(allow, list) or not allow:
    sys.exit("refusing: tools.agentToAgent.enabled is true with no allow list; the reader must not be reachable by other agents (run scripts/ensure-reader-agent.sh to set tools.agentToAgent.allow to " + repr(want["allow"]) + ")")
if any(a in ("reader", "*") or a.endswith("*") for a in allow):
    sys.exit("refusing: tools.agentToAgent.allow " + repr(allow) + " reaches the reader; the reader must not be reachable by other agents")
if not allow_ok:
    sys.exit("refusing: tools.agentToAgent.allow " + repr(sorted(allow)) + " differs from the agents the repo defines " + repr(want["allow"]) + " (run scripts/ensure-reader-agent.sh to converge it)")
if drift:
    sys.exit("refusing: " + "; ".join("agents.entries." + i + ".tools.agentToAgent.send=" + repr(h) + ", shipped " + repr(s) + " in defaults/agents/" + i + "/agent.json" for i, (h, s) in sorted(drift.items())) + " (run scripts/ensure-reader-agent.sh to converge it)")
' "$1"
}
read_cfg tools.agentToAgent.enabled
a2a_on=0
if [ -n "$got" ]; then
  case "$(printf '%s' "$got" | tr -d '[:space:]"')" in
    *true) a2a_on=1 ;;
  esac
fi
if [ "$a2a_on" = 1 ]; then
  # Converge through the config path (idempotent: nothing is written when the live
  # allow list and every live agent's send already are the derived ones).
  read_a2a_live
  if [ "$check_only" = 0 ]; then
    patch="$(a2a_compare patch)"
    if [ -n "$patch" ]; then
      printf '%s' "$patch" | docker exec -i "$GATEWAY" openclaw config patch --stdin >/dev/null
      read_a2a_live
    fi
  fi
  a2a_compare check >&2
fi
bindings="$(docker exec "$GATEWAY" openclaw agents list --json --bindings 2>/dev/null | pick_json '[]')" || bindings="[]"
printf '%s' "$bindings" | python3 -c '
import json, sys
row = next((a for a in json.load(sys.stdin) if a.get("id") == "reader"), None)
if row is None:
    sys.exit("refusing: the reader entry is in the config but not in the agent list")
if row.get("bindings"):
    sys.exit("refusing: the reader has " + str(row["bindings"]) + " channel binding(s); it is scheduled-only (openclaw agents unbind --agent reader --all)")
' >&2

if [ "$check_only" = 0 ]; then
  for f in AGENTS.md SOUL.md IDENTITY.md skills/README.md; do
    install -D -m 0644 "$contract_src/$f" "$workspace/$f"
  done
  echo "contract installed -> $workspace"
  # The one command the reader may run: the door script, by path. Idempotent.
  docker exec "$GATEWAY" openclaw approvals allowlist add --agent "$AGENT" "$door" >/dev/null
fi

# The reader's allowlist is exactly the door script (a read, in both modes).
docker exec "$GATEWAY" openclaw approvals get --json 2>/dev/null | pick_json '*' | python3 -c '
import json, sys
file = json.load(sys.stdin).get("file") or {}
rows = ((file.get("agents") or {}).get("reader") or {}).get("allowlist") or []
patterns = sorted(r.get("pattern") for r in rows)
if patterns != [sys.argv[1]]:
    sys.exit("refusing: the reader exec allowlist is " + repr(patterns) + ", want exactly " + repr([sys.argv[1]]) + " (run scripts/ensure-reader-agent.sh to add the door script; openclaw approvals allowlist remove --agent reader <pattern> for anything else)")
' "$door" >&2

echo "reader agent verified: shipped cap live, no bindings, not reachable by other agents (agent-to-agent allow and each agent's send = the repo's definitions), exec allowlist = the door script only"
echo "verify: scripts/reader-canary/run.sh   # the cap, on a managed claude-cli turn"
