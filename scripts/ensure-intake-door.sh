#!/usr/bin/env bash
# ensure-intake-door.sh - install the intake-door gateway plugin
# (compose/openclaw-gateway/plugins/intake-door) into the gateway's global
# extensions directory, reload it when it changed, and put its `file_intake`
# tool in every agent's tool allow list (docs/runbook.md, "Intake door").
#
# The plugin is the one intake door every agent files through: it hands the
# item to devclaw's file_intake and, when that fails, holds it in the local
# spool it retries, so the door never refuses. platform.patch.json allows and
# enables it; this script carries its files and its allow-list entries.
#
# Allow lists (host state, converged by `openclaw config patch`, add-only):
#   - the global tools.allow, when the live config has one, holds file_intake;
#   - every installed agent with a door holds file_intake in its tools.alsoAllow
#     (or in its tools.allow when its entry ships an explicit allow list).
# An agent with no door is one whose defaults/agents/<id>/agent.json ships
# tools.agentToAgent.send [] (the reader and the study panel): it reads untrusted
# text or answers in a parseable format, holds no write tool, and gets nothing
# here, as with the shared persona blocks (scripts/ensure-dev-problem-block.sh).
#
# The deploy runs it twice (deploy-openclaw.sh, configure phase): `--files`
# before the platform patch, beside fleet-gate, so the restart that patch asks
# for when it first enables the plugin finds the files; then with no argument
# after the agent installs, so a new agent's entry is there to receive the tool.
# `--files` installs and reloads only, and needs neither the plugin enabled nor
# a running gateway. By hand, on the VPS host as lifekit, from the repo checkout:
#   /srv/lifekit-stack/scripts/ensure-intake-door.sh
#   /srv/lifekit-stack/scripts/ensure-intake-door.sh --files   # files + reload only
#   /srv/lifekit-stack/scripts/ensure-intake-door.sh --check   # read-only, exit 1 on drift
#
# The gateway loads a plugin only from files owned by its own uid or root and
# not group- or world-writable, so the files are installed 0644 by the deploy
# account (uid 1000, the gateway's node user). Re-running converges.
#
# Env overrides:
#   OPENCLAW_CONFIG_DIR     host path of the gateway state dir
#                           (default: /srv/openclaw/config, as in compose)
set -euo pipefail
# The production gateway container, never a second labelled one (issue 716).
# shellcheck source=scripts/lib/openclaw-gateway.sh
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/lib/openclaw-gateway.sh"

PLUGIN=intake-door
TOOL=file_intake
FILES="index.js door.js spool.mjs openclaw.plugin.json package.json"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
config_dir="${OPENCLAW_CONFIG_DIR:-/srv/openclaw/config}"
src="${repo_root}/compose/openclaw-gateway/plugins/${PLUGIN}"
dest="${config_dir}/extensions/${PLUGIN}"
check_only=0
files_only=0
case "${1:-}" in
  "") ;;
  --check) check_only=1 ;;
  --files) files_only=1 ;;
  *) echo "usage: $0 [--files|--check]" >&2; exit 2 ;;
esac

changed=0
for f in $FILES; do
  if ! cmp -s "$src/$f" "$dest/$f"; then
    if [ "$check_only" = 1 ]; then
      echo "intake door: $dest/$f differs from the repo" >&2
      exit 1
    fi
    install -D -m 0644 "$src/$f" "$dest/$f"
    changed=1
  fi
done
[ "$check_only" = 1 ] || chmod 0755 "$dest"
if [ "$changed" = 1 ]; then echo "intake door installed -> $dest"; else echo "intake door files unchanged -> $dest"; fi

GATEWAY="$(openclaw_gateway_id_lenient)" || exit 1
if [ -z "$GATEWAY" ]; then
  echo "no running openclaw-gateway container; it loads the plugin when it starts, and the allow lists converge on the next run"
  [ "$check_only" = 1 ] && exit 1
  exit 0
fi

if [ "$changed" = 1 ]; then
  docker exec "$GATEWAY" openclaw plugins reload "$PLUGIN" >/dev/null 2>&1 \
    && echo "reloaded $PLUGIN in the running gateway" \
    || echo "could not reload $PLUGIN (not enabled yet?); the platform patch's restart loads it"
fi
[ "$files_only" = 1 ] && exit 0

# The CLI may interleave log lines with its JSON: keep the first JSON value of
# the wanted shape ("{}" an object, "[]" a list).
pick_json() {
  python3 -c '
import json, sys
want, text = sys.argv[1], sys.stdin.read()
dec, i = json.JSONDecoder(), 0
while (i := text.find(want[0], i)) != -1:
    try:
        obj, end = dec.raw_decode(text, i)
    except ValueError:
        i += 1
        continue
    if isinstance(obj, dict if want == "{}" else list):
        json.dump(obj, sys.stdout)
        sys.exit(0)
    i = end
sys.exit(1)' "$1"
}
absent_re='not found|valid but unset'
read_cfg() { # $1 = config path, $2 = shape; stdout = the value, "null" when unset
  local rc=0 got
  got="$(docker exec "$GATEWAY" openclaw config get "$1" --json 2>&1)" || rc=$?
  if [ "$rc" != 0 ]; then
    printf '%s' "$got" | grep -qiE "$absent_re" || { echo "could not read $1 from the gateway: ${got}" >&2; exit 1; }
    echo null
    return 0
  fi
  printf '%s' "$got" | pick_json "$2" || { echo "could not parse $1 from the gateway: ${got}" >&2; exit 1; }
}

# $1 = patch | check. Prints the add-only patch (nothing when converged), or
# exits 1 naming each list that lacks the tool.
compare() {
  LIVE_ALLOW="$(read_cfg tools.allow '[]')" LIVE_ENTRIES="$(read_cfg agents.entries '{}')" \
    python3 - "$1" "$repo_root" "$TOOL" <<'PY'
import json, os, sys
from pathlib import Path
mode, root, tool = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
allow = json.loads(os.environ["LIVE_ALLOW"])
entries = json.loads(os.environ["LIVE_ENTRIES"]) or {}

def doorless(agent):
    f = root / "defaults/agents" / agent / "agent.json"
    if not f.is_file():
        return False
    s = json.loads(f.read_text())
    for k in ("agents", "entries", agent, "tools", "agentToAgent", "send"):
        s = s.get(k) if isinstance(s, dict) else None
    return s == []

patch, missing = {}, []
if isinstance(allow, list) and tool not in allow:
    patch["tools"] = {"allow": allow + [tool]}
    missing.append("tools.allow")
for agent, e in sorted(entries.items()):
    if not isinstance(e, dict) or doorless(agent):
        continue
    tools = e.get("tools") if isinstance(e.get("tools"), dict) else {}
    key = "allow" if isinstance(tools.get("allow"), list) else "alsoAllow"
    have = tools.get(key) if isinstance(tools.get(key), list) else []
    if tool in have:
        continue
    patch.setdefault("agents", {"entries": {}})["entries"][agent] = {"tools": {key: have + [tool]}}
    missing.append(f"agents.entries.{agent}.tools.{key}")
if mode == "patch":
    if patch:
        json.dump(patch, sys.stdout)
    sys.exit(0)
if missing:
    sys.exit(f"{tool} is missing from: " + ", ".join(missing) + " (run scripts/ensure-intake-door.sh)")
print(f"{tool} is in the global allow list (or there is none) and in every agent with a door")
PY
}

if [ "$check_only" = 0 ]; then
  patch="$(compare patch)"
  if [ -n "$patch" ]; then
    printf '%s' "$patch" | docker exec -i "$GATEWAY" openclaw config patch --stdin >/dev/null
    echo "added $TOOL to the allow lists that lacked it"
  fi
fi
compare check

# Record the installed commit (issue 689; the release gauge reads it) after a
# full run that converged; --check, --files and the no-gateway path never get here.
[ "$check_only" = 1 ] || bash "$(dirname "${BASH_SOURCE[0]}")/lib/install-stamp.sh" intake-door
