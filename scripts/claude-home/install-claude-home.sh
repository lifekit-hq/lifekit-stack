#!/usr/bin/env bash
# install-claude-home.sh — converge the lifekit account's Claude Code home
# (/home/lifekit/.claude, bind-mounted as the gateway's and devclaw's
# ~/.claude) on the vault harness the owner's own machine runs:
#
#   - settings.json: env CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 (provider memory
#     stores are off; the vault is the one store) and the two SessionStart
#     hooks, the vault's bin/session-context.sh and hooks/vault-core.sh. A
#     merge: every other key, and every other hook, is kept as it is.
#   - hooks/vault-core.sh, from defaults/claude-home/hooks/.
#   - CLAUDE.md, the vault pointer from defaults/claude-home/. The file it
#     replaces the first time is kept once as CLAUDE.md.pre-vault-pointer.
#
# The existing auto-memory files under projects/*/memory/ are left alone: the
# env key stops new ones, and retiring the old ones is a separate call.
#
# Runs as the account that owns the directory (lifekit): deploy.sh runs it on
# every deploy, bootstrap-vps.sh through `sudo -u lifekit`. Idempotent; it
# prints one line per file it changed. `lifekit-stack migrate` copies the
# laptop's settings.json over this one; the next deploy merges these keys back.
#
# Env overrides (tests): LIFEKIT_CLAUDE_HOME (default /home/lifekit/.claude).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${HERE}/../../defaults/claude-home"
DST="${LIFEKIT_CLAUDE_HOME:-/home/lifekit/.claude}"

mkdir -p "${DST}/hooks"

if ! cmp -s "${SRC}/hooks/vault-core.sh" "${DST}/hooks/vault-core.sh"; then
  install -m 755 "${SRC}/hooks/vault-core.sh" "${DST}/hooks/vault-core.sh"
  echo "claude home: wrote ${DST}/hooks/vault-core.sh"
fi

if ! cmp -s "${SRC}/CLAUDE.md" "${DST}/CLAUDE.md"; then
  if [[ -f "${DST}/CLAUDE.md" && ! -e "${DST}/CLAUDE.md.pre-vault-pointer" ]]; then
    cp -p "${DST}/CLAUDE.md" "${DST}/CLAUDE.md.pre-vault-pointer"
    echo "claude home: kept the previous CLAUDE.md as CLAUDE.md.pre-vault-pointer"
  fi
  install -m 644 "${SRC}/CLAUDE.md" "${DST}/CLAUDE.md"
  echo "claude home: wrote ${DST}/CLAUDE.md"
fi

python3 - "${DST}/settings.json" <<'PY'
import json
import os
import sys

path = sys.argv[1]
HOOKS = [
    {
        "command": '[ -f "$HOME/memory/bin/session-context.sh" ] && sh "$HOME/memory/bin/session-context.sh" || true',
        "timeout": 10,
    },
    {"command": "$HOME/.claude/hooks/vault-core.sh", "timeout": 5},
]

try:
    with open(path) as f:
        settings = json.load(f)
except FileNotFoundError:
    settings = {}
before = json.dumps(settings, sort_keys=True)

settings.setdefault("env", {})["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
session_start = settings.setdefault("hooks", {}).setdefault("SessionStart", [])
present = {
    h.get("command") for entry in session_start for h in entry.get("hooks", [])
}
for hook in HOOKS:
    if hook["command"] not in present:
        session_start.append(
            {"matcher": "", "hooks": [{"type": "command", **hook}]}
        )

if json.dumps(settings, sort_keys=True) != before:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    if os.path.exists(path):
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
    os.replace(tmp, path)
    print(f"claude home: merged the vault env key and SessionStart hooks into {path}")
PY
