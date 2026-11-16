#!/usr/bin/env bash
# ensure-dev-problem-block.sh - put the one shared "Development-class problems"
# persona block (defaults/agents/shared/dev-problems.md) into the SOUL.md of
# every OpenClaw agent workspace, so each agent files a development-class
# problem through devclaw intake and carries on, never telling the owner
# (docs/runbook.md, "Development-class problems: file and carry on").
#
# One source, one block: the text lives only in the shared file. The script
# finds every workspace under <config>/agents/*/workspace and replaces the
# block between its two marker lines in that workspace's SOUL.md, or appends it
# when the markers are absent. Nothing else in the file is touched, so the
# per-agent persona and Ledger's own notes stay as they are. (Ledger's AGENTS.md
# is composed from finance-sentry and byte-compared by ledger-persona.py; its
# SOUL.md is host state, so the block goes there.)
#
# Run on the VPS host as lifekit, from the repo checkout:
#   /srv/lifekit-stack/scripts/ensure-dev-problem-block.sh [agent-id ...]
# With no ids it covers every workspace found. ensure-learnclaw.sh runs it for
# learnclaw after the deploy rewrites that SOUL.md from git.
#
# Re-running converges. A workspace with no SOUL.md, or whose SOUL.md has a
# lone or reversed marker, is left untouched and reported, and the exit code
# is 1 once the other workspaces are done.
#
# Env overrides:
#   OPENCLAW_CONFIG_DIR     host path of the gateway state dir
#                           (default: /srv/openclaw/config, as in compose)
#   DEV_PROBLEM_BLOCK_FILE  the block to install (default: the shared file)
#   DRY_RUN=1               report what would change, write nothing
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
block="${DEV_PROBLEM_BLOCK_FILE:-${repo_root}/defaults/agents/shared/dev-problems.md}"
config_dir="${OPENCLAW_CONFIG_DIR:-/srv/openclaw/config}"

[ -f "$block" ] || { echo "no block file at $block" >&2; exit 1; }

if [ "$#" -gt 0 ]; then
  agents=("$@")
else
  agents=()
  for ws in "$config_dir"/agents/*/workspace; do
    [ -d "$ws" ] && agents+=("$(basename "$(dirname "$ws")")")
  done
fi
if [ "${#agents[@]}" -eq 0 ]; then
  echo "no agent workspace under ${config_dir}/agents" >&2
  exit 1
fi

# stdout: installed | updated | unchanged | would-install | would-update.
# Exit 3: a lone or reversed marker, file untouched.
splice() { # $1 = SOUL.md path
  python3 - "$1" "$block" "${DRY_RUN:-}" <<'PY'
import os, sys, tempfile

path, block_path, dry = sys.argv[1], sys.argv[2], bool(sys.argv[3])
BEGIN, END = "<!-- lifekit:dev-problems:begin -->", "<!-- lifekit:dev-problems:end -->"
block = open(block_path, encoding="utf-8").read().strip("\n")
text = open(path, encoding="utf-8").read()

b, e = text.find(BEGIN), text.find(END)
if (b < 0) != (e < 0) or text.count(BEGIN) > 1 or text.count(END) > 1 or (b >= 0 and e < b):
    sys.exit(3)
if b >= 0:
    new = text[:b] + block + text[e + len(END):]
    verb = "updated"
else:
    new = text.rstrip("\n") + ("\n\n" if text.strip() else "") + block + "\n"
    verb = "installed"
if new == text:
    print("unchanged")
    sys.exit(0)
if dry:
    print("would-" + ("update" if verb == "updated" else "install"))
    sys.exit(0)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".soul-")
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write(new)
os.chmod(tmp, os.stat(path).st_mode & 0o7777)
os.replace(tmp, path)
print(verb)
PY
}

rc=0
for agent in "${agents[@]}"; do
  soul="${config_dir}/agents/${agent}/workspace/SOUL.md"
  if [ ! -f "$soul" ]; then
    echo "agent '$agent': no SOUL.md at $soul; block not installed" >&2
    rc=1
    continue
  fi
  if verb="$(splice "$soul")"; then
    echo "agent '$agent': dev-problem block $verb -> $soul"
  else
    echo "agent '$agent': $soul has a lone, repeated or reversed block marker; left untouched" >&2
    rc=1
  fi
done
exit "$rc"
